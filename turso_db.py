"""Small synchronous adapter for Turso's documented SQL-over-HTTP API.

Uses a remote libSQL database directly; no local replica or temporary database.
Each production command retains one remote transaction via the API baton.
"""
import base64
import json
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class RemoteDatabaseError(Exception):
    """No credentials, SQL text, or response bodies in user-facing exceptions."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the database token to a redirect target.
        return None


def encode(value):
    if value is None:
        return {'type':'null'}
    if isinstance(value, int):
        return {'type':'integer','value':str(value)}
    if isinstance(value, float):
        return {'type':'float','value':value}
    if isinstance(value, str):
        return {'type':'text','value':value}
    if isinstance(value, bytes):
        return {'type':'blob','base64':base64.b64encode(value).decode()}
    raise TypeError('Unsupported SQL parameter type')


def decode(value):
    kind = value['type']
    if kind == 'null': return None
    if kind == 'integer': return int(value['value'])
    if kind == 'float': return float(value['value'])
    if kind == 'text': return value['value']
    if kind == 'blob': return base64.b64decode(value['base64'])
    raise RemoteDatabaseError('Unexpected database response.')


class Rows:
    def __init__(self, result):
        self.rows = [tuple(decode(v) for v in row) for row in result.get('rows', [])]
        self.position = 0

    def fetchone(self):
        if self.position >= len(self.rows): return None
        row = self.rows[self.position]
        self.position += 1
        return row

    def fetchall(self):
        rows = self.rows[self.position:]
        self.position = len(self.rows)
        return rows


class TursoConnection:
    def __init__(self, url, token, *, opener=None):
        parsed = urlsplit(url.strip())
        if parsed.scheme in ('libsql','turso'):
            parsed = parsed._replace(scheme='https')
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment or
                parsed.path not in ('','/')):
            raise ValueError('TURSO_DATABASE_URL must be a libsql:// or https:// database base URL.')
        if not token or any(c in token for c in '\r\n'):
            raise ValueError('Set TURSO_AUTH_TOKEN to a database read/write token.')
        self.url = urlunsplit(parsed._replace(path='/v2/pipeline'))
        self.token = token
        self.baton = None
        self.in_transaction = False
        self.broken = False
        self.opener = opener or build_opener(NoRedirect())

    def _pipeline(self, requests):
        if self.broken:
            raise RemoteDatabaseError('Database connection interrupted. Retry the same save.')
        body = {'requests':requests}
        if self.baton: body['baton'] = self.baton
        request = Request(self.url, data=json.dumps(body).encode(), method='POST',
                          headers={'Authorization':'Bearer '+self.token,
                                   'Content-Type':'application/json'})
        try:
            with self.opener.open(request, timeout=8) as response:
                data = json.load(response)
            # Honor provider routing without forwarding secrets outside its hosts.
            base = data.get('base_url')
            if base:
                target = urlsplit(base)
                original = urlsplit(self.url)
                trusted = target.hostname == original.hostname or (
                    original.hostname.endswith('.turso.io') and
                    bool(target.hostname) and target.hostname.endswith('.turso.io'))
                if (target.scheme != 'https' or not trusted or target.username or
                        target.password or target.query or target.fragment or
                        target.path not in ('','/','/v2/','/v2/pipeline')):
                    raise ValueError('Unexpected database routing URL')
                self.url = urlunsplit(target._replace(path='/v2/pipeline'))
            self.baton = data.get('baton')
            results = data['results']
            if len(results) != len(requests):
                raise ValueError('Missing results')
            if any(r.get('type') != 'ok' for r in results):
                raise RemoteDatabaseError('Database rejected the operation. Retry the same save; check database status if it persists.')
            return results
        except RemoteDatabaseError:
            raise
        except (HTTPError, URLError, OSError, ValueError, KeyError, TypeError):
            self.broken = True
            raise RemoteDatabaseError('Database unavailable or save not confirmed. Retry the same save.') from None

    def execute(self, sql, parameters=()):
        keyword = sql.strip().split()[0].upper()
        beginning = keyword == 'BEGIN'
        ending = keyword in ('COMMIT','ROLLBACK')
        if beginning and self.in_transaction:
            raise RemoteDatabaseError('Transaction already active.')
        if self.in_transaction and not self.baton:
            raise RemoteDatabaseError('Database transaction expired. Retry the same save.')
        requests = [{'type':'execute','stmt':{'sql':sql,'args':[encode(p) for p in parameters], 'want_rows':True}}]
        keep = beginning or self.in_transaction and not ending
        if not keep: requests.append({'type':'close'})
        result = self._pipeline(requests)[0]['response']['result']
        if keep and not self.baton:
            self.broken = True
            raise RemoteDatabaseError('Database transaction expired. Retry the same save.')
        self.in_transaction = keep
        return Rows(result)

    def execute_group(self, statements):
        """Send related statements in one trip; leave commit to the caller.

        COMMIT is deliberately separate: a SQL error must never commit earlier
        writes from the group. The store rolls the transaction back on error.
        """
        if not statements:
            return []
        keywords = [sql.strip().split()[0].upper() for sql, _ in statements]
        if any(k in ('COMMIT', 'ROLLBACK') for k in keywords):
            raise ValueError('Commit or rollback separately after checking results')
        if 'BEGIN' in keywords[1:] or keywords[0] == 'BEGIN' and self.in_transaction:
            raise RemoteDatabaseError('Transaction already active.')
        if self.in_transaction and not self.baton:
            raise RemoteDatabaseError('Database transaction expired. Retry the same save.')
        keep = self.in_transaction or keywords[0] == 'BEGIN'
        requests = [{'type':'execute', 'stmt':{'sql':sql,
                     'args':[encode(p) for p in parameters], 'want_rows':True}}
                    for sql, parameters in statements]
        if not keep:
            requests.append({'type':'close'})
        # Mark BEGIN before sending, so partial SQL failures still roll back.
        self.in_transaction = keep
        results = self._pipeline(requests)
        if keep and not self.baton:
            self.broken = True
            raise RemoteDatabaseError('Database transaction expired. Retry the same save.')
        return [Rows(result['response']['result']) for result in results[:len(statements)]]

    def executescript(self, sql):
        statement = ''
        for char in sql:
            statement += char
            if char == ';' and sqlite3.complete_statement(statement):
                # journal mode and busy timeout are local SQLite settings.
                if not statement.lstrip().upper().startswith('PRAGMA '):
                    self.execute(statement)
                statement = ''
        if statement.strip():
            raise ValueError('Incomplete database setup statement')

    def commit(self):
        if self.in_transaction: self.execute('COMMIT')

    def rollback(self):
        if self.in_transaction and not self.broken:
            self.execute('ROLLBACK')

    def close(self):
        if self.baton and not self.broken:
            try: self._pipeline([{'type':'close'}])
            except RemoteDatabaseError: pass
        self.baton = None
        self.in_transaction = False
