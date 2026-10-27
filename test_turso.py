"""Turso protocol tests with a real local HTTP gateway and SQLite transactions.

These exercise the remote wire path, not a live third-party cloud account.
"""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from unittest.mock import patch
import server
from turso_db import TursoConnection, RemoteDatabaseError, encode, decode
import test_server as production


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def do_POST(self):
        if self.headers.get('Authorization') != 'Bearer test-db-token':
            self.send_error(401)
            return
        self.server.request_count += 1
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        baton = body.get('baton') or str(uuid.uuid4())
        if baton not in self.server.connections:
            self.server.connections[baton] = sqlite3.connect(self.server.path, isolation_level=None,check_same_thread=False)
        db = self.server.connections[baton]
        results = []
        dropped = False
        for operation in body['requests']:
            try:
                if operation['type'] == 'close':
                    db.close()
                    self.server.connections.pop(baton,None)
                    result = {'type':'close'}
                else:
                    sql = operation['stmt']['sql']
                    if self.server.fail_update and sql.startswith('UPDATE runs SET state='):
                        self.server.fail_update = False
                        raise sqlite3.OperationalError('injected outage')
                    cursor = db.execute(sql,[decode(v) for v in operation['stmt'].get('args',[])])
                    result = {'type':'execute','result':{'rows':[[encode(v) for v in row] for row in cursor.fetchall()]}}
                    if self.server.drop_commit and sql.strip().upper() == 'COMMIT':
                        self.server.drop_commit = False
                        dropped = True
                results.append({'type':'ok','response':result})
            except sqlite3.Error:
                results.append({'type':'error','error':{'message':'simulated SQL error'}})
        if dropped:
            self.close_connection = True
            return
        payload = json.dumps({'baton':baton if baton in self.server.connections else None,'base_url':None,'results':results}).encode()
        self.send_response(200)
        self.send_header('Content-Length',str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class GatewayOpener:
    def __init__(self, address): self.address = address
    def open(self, request, timeout):
        mapped = Request(self.address+'/v2/pipeline',data=request.data,headers=dict(request.header_items()),method='POST')
        return urlopen(mapped,timeout=timeout)


class TursoTests(unittest.TestCase):
    stocked = production.ProductionTests.stocked
    inspected = production.ProductionTests.inspected

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gateway = ThreadingHTTPServer(('127.0.0.1',0),Gateway)
        self.gateway.path = str(Path(self.tmp.name)/'remote.sqlite3')
        self.gateway.connections = {}
        self.gateway.request_count = 0
        self.gateway.fail_update = self.gateway.drop_commit = False
        threading.Thread(target=self.gateway.serve_forever,daemon=True).start()
        opener = GatewayOpener('http://127.0.0.1:'+str(self.gateway.server_port))
        factory = lambda url,token:TursoConnection(url,token,opener=opener)
        self.patch = patch.object(server,'TursoConnection',factory)
        self.patch.start()
        self.store = self.reopen()

    def reopen(self):
        return server.Store(Path(self.tmp.name)/'must-not-create.sqlite3','rover-pin',
                            turso_url='libsql://database.test',turso_token='test-db-token')

    def tearDown(self):
        self.patch.stop()
        self.gateway.shutdown()
        self.gateway.server_close()
        for db in self.gateway.connections.values(): db.close()
        self.tmp.cleanup()

    def req(self, action, **args):
        state = self.store.state()
        return dict(action=action,args=args,operator='Remote tester',requestId=str(uuid.uuid4()),runId=state['runId'],revision=state['revision'])

    def act(self, action, **args):
        return self.store.command(self.req(action,**args))['state']

    def test_label_save_uses_three_round_trips_and_retry_uses_two(self):
        request = self.req('label', qty=5)
        start = self.gateway.request_count
        response = self.store.command(request)
        self.assertEqual(self.gateway.request_count - start, 3)
        self.assertEqual(response['state']['labels'], 5)
        start = self.gateway.request_count
        retry = self.store.command(request)
        self.assertEqual(self.gateway.request_count - start, 2)
        self.assertTrue(retry['duplicate'])
        self.assertEqual(len(retry['state']['events']), 1)
        start = self.gateway.request_count
        self.assertEqual(self.store.state()['labels'], 5)
        self.assertEqual(self.gateway.request_count - start, 0)
        self.assertEqual(self.gateway.connections, {})

    def test_connected_screens_share_an_immutable_committed_snapshot(self):
        self.act('label', qty=7)
        start = self.gateway.request_count
        for _ in range(20):
            snapshot = self.store.state()
            snapshot['labels'] = 999
            snapshot['stations'][0]['empty'] = 999
        self.assertEqual(self.gateway.request_count, start)
        self.assertEqual(self.store.state()['labels'], 7)
        self.assertEqual(self.store.state()['stations'][0]['empty'], 0)

    def test_remote_pin_and_session_secret_persist_without_environment_values(self):
        first = server.Store(self.store.path, turso_url='libsql://database.test',turso_token='test-db-token')
        again = server.Store(self.store.path, turso_url='libsql://database.test',turso_token='test-db-token')
        self.assertEqual(first.safety_pin, 'rover-pin')
        self.assertEqual(first.safety_pin, again.safety_pin)
        self.assertEqual(first.auth_cookie, again.auth_cookie)
        self.assertNotIn(first.safety_pin, again.export('json')[1].decode())

    def test_fifty_action_batch_uses_three_database_trips_and_lost_commit_is_safe(self):
        self.stocked(50)
        state=self.store.state()
        batch=[dict(action='fill',args={'station':1,'mixBatch':'MIX-001'},operator='Batch tester',
                    requestId=str(uuid.uuid4()),runId=state['runId'],revision=state['revision'],queued=True) for _ in range(50)]
        start=self.gateway.request_count
        self.gateway.drop_commit=True
        with self.assertRaises(RemoteDatabaseError):self.store.commands(batch)
        self.assertEqual(self.gateway.request_count-start,3)
        start=self.gateway.request_count
        result=self.store.commands(batch)
        self.assertEqual(self.gateway.request_count-start,3)
        self.assertEqual(len(result['state']['bags']),50)
        self.assertTrue(all(r['duplicate'] for r in result['results']))

    def test_batch_failure_after_event_inserts_rolls_back_all_rows(self):
        state=self.store.state()
        batch=[dict(action='label',args={'qty':1},operator='Batch tester',requestId=str(uuid.uuid4()),
                    runId=state['runId'],revision=state['revision'],queued=True) for _ in range(4)]
        self.gateway.fail_update=True
        with self.assertRaises(RemoteDatabaseError):self.store.commands(batch)
        self.assertEqual(self.store.state()['labels'],0)
        self.assertEqual(len(self.store.commands(batch)['state']['events']),4)

    def test_route_packages_and_receipts_survive_a_lost_batch_commit(self):
        self.act('personAdd',name='Remote stocker',assignments=['delivery:labels:2'])
        self.act('label',qty=3)
        state=self.store.state()
        pickup=dict(action='routePickup',args={'source':'labels','station':2,'qty':3},operator='Remote stocker',requestId=str(uuid.uuid4()),runId=state['runId'],revision=state['revision'],queued=True)
        delivered=dict(action='routeDeliver',args={'delivery':'pending-'+pickup['requestId']},operator='Remote stocker',requestId=str(uuid.uuid4()),runId=state['runId'],revision=state['revision'],queued=True)
        start=self.gateway.request_count;self.gateway.drop_commit=True
        with self.assertRaises(RemoteDatabaseError):self.store.commands([pickup,delivered])
        self.assertEqual(self.gateway.request_count-start,3)
        result=self.store.commands([pickup,delivered])
        self.assertTrue(all(r['duplicate'] for r in result['results']))
        self.assertEqual(len(result['state']['deliveries']),1)
        self.assertEqual(result['state']['deliveries'][0]['status'],'delivered')
        self.assertEqual(result['state']['stations'][1]['empty'],3)

    def test_saved_configuration_and_route_migration_persist_remotely(self):
        self.act('personAdd',name='Remote coordinator',assignments=['coordinator','delivery:hold:1'])
        self.store.upgrade_route_plan()
        request=self.req('configSave',name='Remote saved setup');request['operator']='Remote coordinator'
        config=self.store.command(request)['state']['configs'][0]
        restarted=self.reopen()
        self.assertEqual(restarted.state()['configs'][0]['id'],config['id'])
        self.assertIn('delivery:hold:*',restarted.state()['people'][0]['assignments'])
        count=len(restarted.state()['events']);restarted.upgrade_route_plan()
        self.assertEqual(len(restarted.state()['events']),count)
        self.act('newRun',name='Saved remote run',configId=config['id'],safetyPin='rover-pin')
        self.assertEqual(self.reopen().state()['configs'][0]['id'],config['id'])

    def test_remote_rework_inspection_and_sealing_flow(self):
        self.stocked(1)
        self.act('fill',station=1)
        self.act('move',source='filled',qty=1)
        bag = self.act('receive',source='inspectionTransit',qty=1)['bags'][0]['id']
        self.act('weigh',bag=bag,mass=2.4,unit='oz',labelOK=True)
        self.act('move',source='hold',qty=1)
        self.act('receive',source='reworkTransit',qty=1)
        self.act('clear',bag=bag,mass=2.1,unit='oz')
        self.act('move',source='returnReady',qty=1)
        self.act('receive',source='returnTransit',qty=1)
        self.act('seal',bag=bag)
        self.assertEqual(self.store.state()['bags'][0]['status'],'sealed')
        self.assertFalse(Path(self.store.path).exists())
        self.assertEqual(self.reopen().state()['revision'],self.store.state()['revision'])

    def test_failure_after_event_insert_rolls_back_all_tables(self):
        request = self.req('label',qty=3)
        before = self.store.state()
        self.gateway.fail_update = True
        with self.assertRaises(RemoteDatabaseError): self.store.command(request)
        self.assertEqual(before,self.store.state())
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone()[0],0)
        self.store.command(request)
        self.assertEqual(self.store.state()['labels'],3)

    def test_lost_commit_response_retries_without_duplicate(self):
        request = self.req('label',qty=4)
        self.gateway.drop_commit = True
        with self.assertRaises(RemoteDatabaseError): self.store.command(request)
        result = self.store.command(request)
        self.assertTrue(result['duplicate'])
        self.assertEqual(result['state']['labels'],4)
        self.assertEqual(len(result['state']['events']),1)

    def test_history_protection_and_archive_survive_new_backend(self):
        self.act('label',qty=2)
        old_run = self.store.state()['runId']
        with self.store.connect() as db:
            with self.assertRaises(RemoteDatabaseError): db.execute('DELETE FROM events')
        self.act('reconcile',note='Unused bags returned',safetyPin='rover-pin')
        self.act('newRun',name='Cloud Run 2',safetyPin='rover-pin')
        reopened = self.reopen()
        self.assertEqual(reopened.state()['batch'],'Cloud Run 2')
        self.assertEqual(len(reopened.state(old_run)['events']),3)
        for kind in ('events','weights','bags','people','messages','devices','json'):
            self.assertTrue(reopened.export(kind,old_run)[1])

    def test_missing_credentials_and_insecure_urls_fail_closed(self):
        with self.assertRaises(ValueError): server.Store('unused','pin',turso_url='libsql://db.test')
        for url in ('http://db.test','https://user:secret@db.test','https://db.test?token=secret'):
            with self.assertRaises(ValueError): TursoConnection(url,'token')
        for value in (None,2**60,2.1,'word',b'bytes'):
            self.assertEqual(decode(encode(value)),value)
