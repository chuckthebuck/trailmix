"""Real HTTP authentication, secure cookies, and push updates without polling."""
import http.client
import json
import os
import socket
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from server import Store, Handler, ThreadingHTTPServer
from turso_db import RemoteDatabaseError


class QuietHandler(Handler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        try:
            super().do_GET()
        finally:
            if self.path.startswith('/api/stream'):
                self.server.stream_done.set()


class FastKeepalive(threading.Condition):
    # Exercise disconnect cleanup without waiting for the 20-second idle ping.
    def wait_for(self, predicate, timeout=None):
        return super().wait_for(predicate, min(timeout or 0.1, 0.1))


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/'live.sqlite3', 'test-safety')
        self.store.changed = FastKeepalive()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.server.store = self.store
        self.server.require_access_code = True
        self.server.access_code = 'test-access'
        self.server.auth_cookie = self.store.auth_cookie
        self.server.login_failures = self.server.login_blocked_until = 0
        self.server.stream_done = threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.tmp.cleanup()

    def request(self, method, path, body=None, cookie=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        headers = {'Content-Type':'application/json','X-TrailMix':'1'}
        if cookie:
            headers['Cookie'] = cookie
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        status, headers, data = response.status, response.getheaders(), response.read()
        conn.close()
        return status, dict(headers), json.loads(data)

    def command(self, qty):
        s = self.store.state()
        return dict(action='label',args={'qty':qty},operator='HTTP tester',
                    revision=s['revision'],runId=s['runId'],requestId=str(uuid.uuid4()))

    def test_https_login_cookie_and_stream_requires_authentication(self):
        status, _, _ = self.request('GET','/api/stream')
        self.assertEqual(status,401)
        with patch.dict(os.environ, {'TRAILMIX_HTTPS':'1'}):
            status, headers, _ = self.request('POST','/api/login',{'code':'test-access'})
        self.assertEqual(status,200)
        for flag in ('Secure','HttpOnly','SameSite=Strict'):
            self.assertIn(flag,headers['Set-Cookie'])
        status, _, _ = self.request('POST','/api/login',{'code':'wrong'})
        self.assertEqual(status,401)

    def test_open_access_needs_no_code_or_cookie_but_safety_keeps_pin(self):
        self.server.require_access_code = False
        status, _, state = self.request('GET', '/api/state')
        self.assertEqual(status, 200)
        status, _, saved = self.request('POST', '/api/action', self.command(2))
        self.assertEqual(status, 200)
        self.assertEqual(saved['state']['labels'], 2)
        request = self.command(1)
        request.update(action='reconcile', args={'note':'Review unused stock','safetyPin':'wrong'})
        self.assertEqual(self.request('POST','/api/action',request)[0],409)
        request['args']['safetyPin'] = 'test-safety'
        self.assertEqual(self.request('POST','/api/action',request)[0],200)
        status, headers, _ = self.request('POST','/api/login',{})
        self.assertEqual(status,200)
        self.assertNotIn('Set-Cookie',headers)

    def test_optional_login_cookie_survives_store_restart(self):
        _, headers, _ = self.request('POST','/api/login',{'code':'test-access'})
        cookie = headers['Set-Cookie'].split(';')[0]
        restarted = Store(self.store.path)
        self.server.store = restarted
        self.server.auth_cookie = restarted.auth_cookie
        self.assertEqual(self.request('GET','/api/state',cookie=cookie)[0],200)
        self.assertEqual(restarted.safety_pin,'test-safety')

    def test_database_outage_returns_retryable_without_leaking_details(self):
        _, headers, _ = self.request('POST','/api/login',{'code':'test-access'})
        cookie = headers['Set-Cookie'].split(';')[0]
        with patch.object(self.store, 'command', side_effect=RemoteDatabaseError('private database details')):
            status, _, body = self.request('POST','/api/action',self.command(1),cookie)
        self.assertEqual(status,503)
        self.assertTrue(body['retryable'])
        self.assertNotIn('private',json.dumps(body))
        with patch.object(self.store, 'state', side_effect=RemoteDatabaseError('private database details')):
            status, _, body = self.request('GET','/api/state',cookie=cookie)
        self.assertEqual(status,503)
        self.assertTrue(body['retryable'])

    def test_one_connection_receives_initial_state_and_saved_changes(self):
        _, headers, _ = self.request('POST','/api/login',{'code':'test-access'})
        cookie = headers['Set-Cookie'].split(';')[0]
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        conn.request('GET','/api/stream',headers={'Cookie':cookie})
        stream_socket = conn.sock
        response = conn.getresponse()
        self.assertEqual(response.status,200)
        self.assertEqual(response.getheader('Content-Type'),'text/event-stream')

        def next_state():
            for _ in range(12):
                line = response.fp.readline().decode().strip()
                if line.startswith('data: '):
                    return json.loads(line[6:])
            self.fail('Missing SSE state')

        try:
            first = next_state()
            status, _, saved = self.request('POST','/api/action',self.command(3),cookie)
            self.assertEqual(status,200)
            pushed = next_state()
            self.assertEqual(pushed['revision'],saved['state']['revision'])
            self.assertEqual(pushed['labels'],first['labels']+3)
            self.assertGreater(pushed['revision'],first['revision'])
        finally:
            stream_socket.shutdown(socket.SHUT_RDWR)
            response.close()
            conn.close()
            self.store.command(self.command(1))
            self.assertTrue(self.server.stream_done.wait(3),'Stream did not stop after disconnect')


if __name__ == '__main__':
    unittest.main()
