"""Compiled Go backend tests using real HTTP and temporary SQLite libSQL gateway."""
import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import uuid
from go_gateway import start_gateway
import server as legacy

ROOT=Path(__file__).resolve().parent
BINARY=os.getenv('TRAILMIX_GO_BINARY','/tmp/trailmix-go')


class GoBackendTests(unittest.TestCase):
    def setUp(self):
        if not Path(BINARY).is_file():
            self.fail('Build the Go backend first: go build -buildvcs=false -o /tmp/trailmix-go .')
        self.tmp=tempfile.TemporaryDirectory();self.db=Path(self.tmp.name)/'data.sqlite3'
        self.gateway=start_gateway(self.db);self.proc=None
        self.logs=[];self.reader=None

    def tearDown(self):
        self.stop()
        self.gateway.shutdown();self.gateway.server_close()
        for db in self.gateway.connections.values():
            db.close()
        self.tmp.cleanup()

    def start(self,**overrides):
        env=dict(os.environ,TURSO_DATABASE_URL='http://127.0.0.1:'+str(self.gateway.server_port),TURSO_AUTH_TOKEN='test-db-token',TRAILMIX_TEST_DB='1',TRAILMIX_SEED_ROSTER='0',TRAILMIX_UPGRADE_WORKFLOW='0',TRAILMIX_REQUIRE_ACCESS_CODE='0',TRAILMIX_SAFETY_PIN='',TRAILMIX_HTTPS='0')
        env.update(overrides)
        self.proc=subprocess.Popen([BINARY,'--host','127.0.0.1','--port','0'],cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        for _ in range(20):
            line=self.proc.stdout.readline()
            self.logs.append(line)
            match=re.search(r'http://127.0.0.1:(\d+)',line)
            if match:
                self.port=int(match[1]);break
            if not line:raise AssertionError('Go backend failed to start: '+''.join(self.logs))
        else:raise AssertionError('No startup marker')
        def drain():
            for line in self.proc.stdout:self.logs.append(line)
        self.reader=threading.Thread(target=drain,daemon=True);self.reader.start()
        return self.request('GET','/api/state')[1]

    def stop(self):
        if self.proc:
            self.proc.terminate();self.proc.wait(timeout=15)
            if self.reader:self.reader.join(timeout=2)
            self.proc.stdout.close();self.proc=None

    def request(self,method,path,body=None,delta=False):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=30)
        headers={'Content-Type':'application/json','X-TrailMix':'1'}
        if delta:headers['X-TrailMix-Sync']='delta-v1'
        conn.request(method,path,json.dumps(body) if body is not None else None,headers)
        response=conn.getresponse();status=response.status;raw=response.read();conn.close()
        return status,json.loads(raw) if raw else None

    def req(self,action,**args):
        state=self.request('GET','/api/state')[1]
        return dict(action=action,args=args,operator='Tester',requestId=str(uuid.uuid4()),runId=state['runId'],revision=state['revision'])

    def act(self,action,**args):
        request=self.req(action,**args)
        status,data=self.request('POST','/api/action',request)
        self.assertEqual(status,200,data)
        return data['state']

    def batch(self,requests,delta=True):
        for request in requests:request['queued']=True
        return self.request('POST','/api/actions',{'commands':requests},delta)

    def seed_legacy(self):
        store=legacy.Store(self.db,'keep-pin')
        state=store.state()
        request=dict(action='label',args={'qty':5},operator='Operator 🥜',requestId='pending-before-go',runId=state['runId'],revision=state['revision'],queued=True,recordedStation='Labeling')
        store.commands([request]);state=store.state()
        # Test the compressed format currently deployed in production.
        store.turso_url='packing-only'
        with sqlite3.connect(self.db) as db:db.execute('UPDATE runs SET state=? WHERE id=?',(store.encode_state(state),state['runId']))
        return state,request,store.auth_cookie

    def test_compressed_migration_preserves_pending_ids_pin_history_and_restart(self):
        expected,request,cookie=self.seed_legacy()
        state=self.start()
        self.assertEqual(state['labels'],5);self.assertEqual(state['runId'],expected['runId'])
        status,data=self.batch([request]);self.assertEqual(status,200,data)
        self.assertTrue(data['results'][0]['duplicate']);self.assertEqual(data['databaseTrips'],2)
        status,exported=self.request('GET','/api/export?kind=json');self.assertEqual(status,200)
        self.assertEqual(exported['events'],expected['events'])
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='safety_pin'").fetchone()[0],'keep-pin')
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='auth_cookie'").fetchone()[0],cookie)
            self.assertEqual(json.loads(db.execute('SELECT state FROM runs').fetchone()[0])['trailmixStorage'],'zlib-v1')
        self.stop();state=self.start();self.assertEqual(state['labels'],5)
        self.assertTrue(self.batch([request])[1]['results'][0]['duplicate'])

    def test_fifty_new_actions_three_trips_changed_records_and_small_patch(self):
        self.start(TRAILMIX_SAFETY_PIN='test-pin')
        self.act('label',qty=50);state=self.act('labelPickup',qty=50,station=1)
        self.act('receiveLabels',movement=state['moves'][0]['id']);self.act('assignBatch',station=1,batch='MIX')
        first=self.req('fill',station=1,mixBatch='MIX');batch=[dict(first,requestId=str(uuid.uuid4())) for _ in range(50)]
        start=len(self.gateway.statements)
        status,data=self.batch(batch)
        self.assertEqual(status,200,data);self.assertEqual(data['databaseTrips'],3)
        self.assertNotIn('state',data);self.assertEqual(len(data['delta']['entities']['bags']['upsert']),50)
        queries=[sql for sql,args in self.gateway.statements[start:]]
        self.assertFalse(any('SELECT state FROM runs' in sql or 'UPDATE runs SET state' in sql for sql in queries))
        self.assertFalse(any('SELECT result FROM events' in sql for sql in queries))
        self.assertLess(len(json.dumps(data)),150000)
        self.assertLess(data['databaseRequestBytes'],180000)
        self.assertEqual(len(self.request('GET','/api/state')[1]['bags']),50)

    def test_lost_commit_then_retry_has_no_duplicate_actions(self):
        self.start();request=self.req('label',qty=4);self.gateway.drop_commit=True
        status,data=self.batch([request]);self.assertEqual(status,503);self.assertTrue(data['retryable'])
        status,data=self.batch([request]);self.assertEqual(status,200,data);self.assertTrue(data['results'][0]['duplicate'])
        exported=self.request('GET','/api/export?kind=json')[1]
        self.assertEqual(exported['labels'],4);self.assertEqual(len(exported['events']),1)

    def test_partial_sql_failure_rolls_back_audit_entities_requests_and_header(self):
        self.start();request=self.req('label',qty=4);self.gateway.fail_prefix='UPDATE tm_runs'
        status,_=self.batch([request]);self.assertEqual(status,503)
        with sqlite3.connect(self.db) as db:
            for table in ('events','requests','tm_event_index','tm_results'):
                self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
        status,data=self.batch([request]);self.assertEqual(status,200,data);self.assertFalse(data['results'][0]['duplicate'])
        self.assertEqual(self.request('GET','/api/state')[1]['labels'],4)

    def test_invalid_second_action_rolls_back_whole_batch(self):
        self.start();one=self.req('label',qty=4);two=self.req('fill',station=1,mixBatch='MIX')
        status,_=self.batch([one,two]);self.assertEqual(status,409)
        state=self.request('GET','/api/state')[1];self.assertEqual(state['labels'],0);self.assertEqual(state['events'],[])

    def test_reads_do_not_wait_behind_remote_write_transaction(self):
        self.start();request=self.req('label',qty=1)
        entered,release=threading.Event(),threading.Event();self.gateway.gate=(entered,release)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            saving=pool.submit(self.batch,[request]);self.assertTrue(entered.wait(5))
            try:
                state=self.request('GET','/api/state')[1]
                self.assertEqual(state['labels'],0);self.assertFalse(saving.done())
            finally:release.set()
            self.assertEqual(saving.result()[0],200)
        self.gateway.gate=None;self.assertEqual(self.request('GET','/api/state')[1]['labels'],1)

    def test_saved_config_new_run_archives_and_exports_remain_complete(self):
        self.start(TRAILMIX_SAFETY_PIN='keep-pin')
        self.act('personAdd',name='Tester',assignments=['coordinator','fill:2'])
        state=self.act('configSave',name='Morning');config=state['configs'][0]['id'];old=state['runId']
        state=self.act('newRun',name='Next run',configId=config,safetyPin='keep-pin')
        self.assertNotEqual(state['runId'],old);self.assertEqual(state['events'],[])
        self.assertEqual(state['people'][0]['assignments'],['coordinator','fill:2'])
        self.assertEqual(self.request('GET','/api/runs')[1][1]['archived'],True)
        archive=self.request('GET','/api/export?kind=json&run='+old)[1]
        self.assertEqual(archive['events'][-1]['action'],'newRun')
        self.assertIn('before',archive['events'][0]);self.assertNotIn('safetyPin',archive['events'][-1]['data'])
        self.stop();self.assertEqual(self.start()['batch'],'Next run')

    def test_same_request_id_different_payload_rejected(self):
        self.start();request=self.req('label',qty=3)
        self.assertEqual(self.batch([request])[0],200)
        request['args']['qty']=5;status,_=self.batch([request]);self.assertEqual(status,409)
        self.assertEqual(self.request('GET','/api/state')[1]['labels'],3)

    def test_no_sql_snapshots_on_next_start(self):
        self.seed_legacy();self.start();self.stop();start=len(self.gateway.statements);self.start()
        queries=self.gateway.statements[start:]
        legacy_selects=[(sql,args) for sql,args in queries if 'SELECT id,state,archived FROM runs' in sql]
        self.assertEqual(len(legacy_selects),1)
        self.assertIn('NOT IN (SELECT id FROM tm_runs)',legacy_selects[0][0])

    def test_sse_sends_revisioned_patches_after_initial_snapshot(self):
        self.start()
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        conn.request('GET','/api/stream?deviceId=dev-sse-test&sync=delta-v1')
        response=conn.getresponse();self.assertEqual(response.status,200)
        def frame():
            kind=None;data=None
            for _ in range(12):
                line=response.readline().decode().rstrip('\r\n')
                if line.startswith('event: '):kind=line[7:]
                if line.startswith('data: '):data=json.loads(line[6:])
                if not line and kind:return kind,data
            self.fail('Incomplete SSE frame')
        try:
            kind,initial=frame();self.assertEqual(kind,'state');self.assertEqual(initial['revision'],0)
            request=self.req('label',qty=2);self.assertEqual(self.batch([request])[0],200)
            kind,patch=frame();self.assertEqual(kind,'patch');self.assertEqual(patch['baseRevision'],0)
            self.assertEqual(patch['revision'],1);self.assertEqual(patch['core']['labels'],2)
            self.assertEqual(len(patch['eventsAppend']),1);self.assertNotIn('state',patch)
            self.assertLess(len(json.dumps(patch)),3000)
        finally:
            conn.close();response.close()

    def test_startup_upgrades_are_once_only_and_pin_persists(self):
        state=self.start(TRAILMIX_SEED_ROSTER='1',TRAILMIX_UPGRADE_WORKFLOW='1',TRAILMIX_SAFETY_PIN='seed-pin')
        self.assertEqual(len(state['people']),28)
        self.assertEqual({p['name'] for p in state['people'] if p['assignments']==['safety']},{'Lucas','Blake','Charlie'})
        revision=state['revision'];self.stop()
        state=self.start(TRAILMIX_SEED_ROSTER='1',TRAILMIX_UPGRADE_WORKFLOW='1')
        self.assertEqual(state['revision'],revision)
        self.assertEqual(len(state['people']),28)
        with sqlite3.connect(self.db) as db:self.assertEqual(db.execute("SELECT value FROM meta WHERE key='safety_pin'").fetchone()[0],'seed-pin')

    def test_bad_types_and_missing_content_validation_rejected(self):
        self.start()
        for field,value in [('requestId',12345678),('revision',0.0),('operator',1234)]:
            request=self.req('label',qty=1);request[field]=value
            self.assertEqual(self.batch([request])[0],409)
        self.assertEqual(self.batch([self.req('label',qty=1.5)])[0],409)
        self.assertEqual(self.request('GET','/api/state')[1]['labels'],0)

    def test_legacy_python_cannot_write_stale_run_after_migration(self):
        expected,_,_=self.seed_legacy();self.start()
        with sqlite3.connect(self.db) as db:
            with self.assertRaisesRegex(sqlite3.IntegrityError,'migrated to Go'):
                db.execute('UPDATE runs SET state=? WHERE id=?',(json.dumps(expected),expected['runId']))
        with self.assertRaisesRegex(RuntimeError,'migrated to the Go'):
            legacy.Store(self.db,'keep-pin')
        self.assertEqual(self.act('label',qty=1)['labels'],6)

    def test_lost_migration_commit_restarts_without_repeating_history(self):
        expected,request,_=self.seed_legacy();self.gateway.drop_commit=True
        with self.assertRaisesRegex(AssertionError,'failed to start'):
            self.start()
        self.stop();state=self.start()
        self.assertEqual(state['labels'],expected['labels'])
        self.assertTrue(self.batch([request])[1]['results'][0]['duplicate'])
        self.assertEqual(len(self.request('GET','/api/export?kind=json')[1]['events']),1)

    def test_failed_migration_rolls_back_and_original_snapshot_survives(self):
        expected,_,_=self.seed_legacy();self.gateway.fail_prefix='INSERT INTO tm_event_index'
        with self.assertRaisesRegex(AssertionError,'failed to start'):
            self.start()
        self.stop()
        with sqlite3.connect(self.db) as db:
            self.assertIsNone(db.execute("SELECT value FROM meta WHERE key='tm_storage_version'").fetchone())
            self.assertEqual(legacy.Store.decode_state(db.execute('SELECT state FROM runs').fetchone())['bags'],expected['bags'])
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],1)
        self.assertEqual(self.start()['labels'],expected['labels'])
