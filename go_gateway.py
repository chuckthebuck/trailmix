"""Development-only libSQL HTTP gateway backed by temporary SQLite."""
import json
import sqlite3
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from turso_db import encode, decode


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.headers.get('Authorization') != 'Bearer test-db-token':
            self.send_error(401)
            return
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.pipelines.append(body)
        self.server.request_count += 1
        baton = body.get('baton') or str(uuid.uuid4())
        if baton not in self.server.connections:
            self.server.connections[baton] = sqlite3.connect(self.server.path,isolation_level=None,check_same_thread=False,timeout=10)
        db = self.server.connections[baton]
        results, dropped = [], False
        for operation in body['requests']:
            try:
                if operation['type'] == 'close':
                    db.close();self.server.connections.pop(baton,None)
                    result = {'type':'close'}
                else:
                    statement = operation['stmt']
                    query,parameters = statement['sql'],[decode(v) for v in statement.get('args',[])]
                    self.server.statements.append((query,parameters))
                    if self.server.fail_prefix and query.startswith(self.server.fail_prefix):
                        self.server.fail_prefix = None
                        raise sqlite3.OperationalError('injected write failure')
                    if self.server.gate and query.startswith('UPDATE tm_runs'):
                        entered,release = self.server.gate
                        entered.set();release.wait(10)
                    cursor = db.execute(query,parameters)
                    rows=cursor.fetchall() if statement.get('want_rows',True) else []
                    result={'type':'execute','result':{'rows':[[encode(v) for v in row] for row in rows]}}
                    if self.server.drop_commit and query.strip().upper() == 'COMMIT':
                        self.server.drop_commit=False;dropped=True
                results.append({'type':'ok','response':result})
            except sqlite3.Error:
                results.append({'type':'error','error':{'message':'simulated SQL error'}})
        if dropped:
            self.close_connection=True
            return
        payload=json.dumps({'baton':baton if baton in self.server.connections else None,'results':results}).encode()
        self.send_response(200);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)


def start_gateway(path):
    server=ThreadingHTTPServer(('127.0.0.1',0),Gateway)
    server.path=str(path);server.connections={};server.pipelines=[];server.statements=[];server.request_count=0
    server.fail_prefix=None;server.drop_commit=False;server.gate=None
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server
