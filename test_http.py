"""Lost responses must preserve committed batches and duplicate-safe retries."""
import contextlib
import io
import json
import types
import unittest
from unittest.mock import patch
import test_server as production
from server import Handler


class DisconnectedWriter:
    def __init__(self, fail_on, error):
        self.writes, self.fail_on, self.error = 0, fail_on, error

    def write(self, body):
        self.writes += 1
        if self.writes >= self.fail_on:
            raise self.error('Client disconnected')
        return len(body)


class ResponseTests(unittest.TestCase):
    setUp = production.ProductionTests.setUp
    tearDown = production.ProductionTests.tearDown
    req = production.ProductionTests.req

    def handler(self, payload, writer):
        body = json.dumps(payload).encode()
        h = Handler.__new__(Handler)
        h.server = types.SimpleNamespace(store=self.store, require_access_code=False)
        h.headers = {'Content-Type':'application/json', 'X-TrailMix':'1', 'Content-Length':str(len(body))}
        h.path, h.request_version = '/api/actions', 'HTTP/1.1'
        h.requestline, h.command = 'POST /api/actions HTTP/1.1', 'POST'
        h.rfile, h.wfile = io.BytesIO(body), writer
        h.close_connection = False
        h.log_message = lambda *args: None
        return h

    def test_disconnect_keeps_batch_and_retry_exactly_once(self):
        for error in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            for fail_on in (1, 2):
                with self.subTest(error=error, fail_on=fail_on):
                    req = self.req('label', qty=2)
                    req['queued'] = True
                    before = self.store.state()
                    writer = DisconnectedWriter(fail_on, error)
                    h = self.handler({'commands':[req]}, writer)
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        h.do_POST()
                    self.assertEqual(stderr.getvalue(), '')
                    self.assertTrue(h.close_connection)
                    self.assertEqual(writer.writes, fail_on)  # No second response.
                    committed = self.store.state()
                    self.assertEqual(committed['revision'], before['revision']+1)
                    retry = self.store.commands([req])
                    self.assertTrue(retry['results'][0]['duplicate'])
                    self.assertEqual(retry['state']['revision'], committed['revision'])
                    self.assertEqual(len([e for e in retry['state']['events'] if e['requestId']==req['requestId']]), 1)

    def test_failure_after_commit_does_not_claim_rollback(self):
        req = self.req('label', qty=3)
        req['queued'] = True
        commands = self.store.commands
        def lost_confirmation(requests):
            commands(requests)
            raise RuntimeError('Failure after commit')
        h = self.handler({'commands':[req]}, io.BytesIO())
        with patch.object(self.store, 'commands', side_effect=lost_confirmation):
            with contextlib.redirect_stderr(io.StringIO()):
                h.do_POST()
        response = h.wfile.getvalue().split(b'\r\n\r\n', 1)
        self.assertIn(b'500', response[0])
        data = json.loads(response[1])
        self.assertTrue(data['retryable'])
        self.assertIn('Retry the same save', data['error'])
        self.assertNotIn('No partial', data['error'])
        self.assertTrue(commands([req])['results'][0]['duplicate'])

    def test_disconnect_reading_request_has_no_error_response(self):
        h = self.handler({}, io.BytesIO())
        h.rfile = types.SimpleNamespace(read=lambda _: (_ for _ in ()).throw(ConnectionResetError()))
        h.do_POST()
        self.assertTrue(h.close_connection)
        self.assertEqual(h.wfile.getvalue(), b'')

    def test_stream_disconnect_during_headers_retry_or_initial_state(self):
        for error in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            for fail_on in (1, 2, 3):
                with self.subTest(error=error, fail_on=fail_on):
                    writer = DisconnectedWriter(fail_on, error)
                    h = self.handler({}, writer)
                    h.command, h.path = 'GET', '/api/stream'
                    h.do_GET()
                    self.assertTrue(h.close_connection)
                    self.assertEqual(writer.writes, fail_on)

    def test_head_probe_returns_page_headers_without_body(self):
        for path, status in (('/', 200), ('/index.html', 200), ('/api/stream', 404)):
            with self.subTest(path=path):
                h = self.handler({}, io.BytesIO())
                h.command, h.path = 'HEAD', path
                h.do_HEAD()
                headers, body = h.wfile.getvalue().split(b'\r\n\r\n', 1)
                self.assertIn(str(status).encode(), headers.split(b'\r\n')[0])
                self.assertIn(b'Content-Length:', headers)
                self.assertEqual(body, b'')
