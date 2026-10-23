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

    def test_remote_rework_inspection_and_sealing_flow(self):
        self.stocked(1)
        self.act('fill',station=1)
        self.act('move',source='filled',qty=1)
        bag = self.act('receive',source='inspectionTransit',qty=1)['bags'][0]['id']
        self.act('weigh',bag=bag,mass=1.9,unit='oz',labelOK=True)
        self.act('move',source='hold',qty=1)
        self.act('receive',source='reworkTransit',qty=1)
        self.act('clear',bag=bag,mass=1.6,unit='oz')
        self.act('move',source='returnReady',qty=1)
        self.act('receive',source='returnTransit',qty=1)
        self.act('weigh',bag=bag,mass=1.6,unit='oz',labelOK=True)
        self.act('move',source='ready',qty=1)
        self.act('receive',source='sealTransit',qty=1)
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
        for value in (None,2**60,1.6,'word',b'bytes'):
            self.assertEqual(decode(encode(value)),value)
