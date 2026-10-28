#!/usr/bin/env python3
"""TrailMix: browser stations, SQLite persistence, append-only production records."""
import argparse
import base64
import copy
import csv
import hashlib
import hmac
import io
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import time
import threading
import uuid
import zlib
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from turso_db import TursoConnection, RemoteDatabaseError

ROOT = Path(__file__).resolve().parent
OZ = 28.349523125
NOW = lambda: int(time.time() * 1000)
DELIVERY_LEGS = {'labels':('Labeling','Filling','stocker',None),
 'filled':('Filling','First inspection','firstMover','inspectionTransit'),
 'hold':('First inspection','Rework','finalMover','reworkTransit'),
 'returnReady':('Rework','Sealing','finalMover','returnTransit'),
 'ready':('First inspection','Sealing','finalMover','sealTransit'),
 'sealed':('Sealing','Final inspection','finalMover','finalTransit')}

def route_allowed(person, source, station):
    if not person or person.get('deletedAt') or source not in DELIVERY_LEGS:
        return False
    roles=person['assignments']
    if source not in ('labels','filled'):
        return DELIVERY_LEGS[source][2] in roles or any(r.startswith('delivery:'+source+':') for r in roles)
    return (DELIVERY_LEGS[source][2] in roles or 'delivery:'+source+':*' in roles or
            'delivery:'+source+':'+str(station) in roles)

QUEUED_ACTIONS = {'routePickup', 'routeDeliver', 'label', 'labelPickup', 'receiveLabels', 'fill', 'move', 'receive',
                  'weigh', 'clear', 'seal', 'failSeal', 'finish', 'finalFail'}
CLASS_ROSTER = ['Truus','Leslie','Blake','David','Brian','Aiden','Megan','Fabio','Katelyn','Elias','Halen','Dylan','Luke','Hailey','Marie','Keira','Natalie','Charlie','Beaton','Veena','Nico','Aum','Alexander','Martin','Logan','Lucas','Vito','Sarah']
SAFETY_ROSTER = {'lucas','blake','charlie'}

STAFF_ACTIONS = {'assignAtStation', 'configSave', 'personDelete', 'personRestore', 'rosterImport', 'personAddMany', 'personAdd', 'personUpdate', 'messagePost', 'messageAcknowledge', 'messageResolve',
                 'deviceRegister', 'deviceUpdate'}


def fresh():
    return dict(version=2, revision=0, runId=str(uuid.uuid4()), batch='Trail mix · Run 01',
                started=NOW(), demo=False, limits=dict(min=2.0, max=2.25, confirmed=True),
                stations=[dict(id=i, empty=0, batch='') for i in range(1, 4)],
                labels=0, moves=[], bags=[], events=[], next=1, safetyHold=False,
                people=[], messages=[], devices=[], deliveries=[], configs=[])


class Problem(Exception):
    pass


class Store:
    def __init__(self, path, safety_pin=None, *, turso_url=None, turso_token=None):
        if bool(turso_url) != bool(turso_token):
            raise ValueError("Set both TURSO_DATABASE_URL and TURSO_AUTH_TOKEN; refusing a local fallback.")
        self.turso_url, self.turso_token = turso_url, turso_token
        self.database_lock = threading.RLock()
        self.path, self.safety_pin = str(path), safety_pin
        self.presence_lock = threading.Lock()
        self.changed = threading.Condition()
        self.serial = 0
        self.presence = {}
        self.cached_current = None
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, state TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                  run_id TEXT NOT NULL, at INTEGER NOT NULL, operator TEXT NOT NULL, action TEXT NOT NULL,
                  payload TEXT NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, digest TEXT NOT NULL, run_id TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'Event history is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'Event history is append-only'); END;
            ''')
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT value FROM meta WHERE key='tm_storage_version'").fetchone():
                raise RuntimeError('This database has been migrated to the Go backend. Start the Go service; Python cannot write it.')
            saved_pin = db.execute("SELECT value FROM meta WHERE key='safety_pin'").fetchone()
            configured_pin = str(safety_pin).strip() if safety_pin else None
            self.pin_created = not saved_pin and not configured_pin
            self.safety_pin = configured_pin or (saved_pin[0] if saved_pin else secrets.token_hex(4))
            if not saved_pin or saved_pin[0] != self.safety_pin:
                db.execute("INSERT INTO meta(key,value) VALUES ('safety_pin',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (self.safety_pin,))
            saved_cookie = db.execute("SELECT value FROM meta WHERE key='auth_cookie'").fetchone()
            self.auth_cookie = saved_cookie[0] if saved_cookie else secrets.token_urlsafe(32)
            if not saved_cookie:
                db.execute("INSERT INTO meta(key,value) VALUES ('auth_cookie',?)", (self.auth_cookie,))
            if not db.execute("SELECT value FROM meta WHERE key='current'").fetchone():
                s = fresh()
                db.execute('INSERT INTO runs(id,state) VALUES (?,?)', (s['runId'], self.encode_state(s)))
                db.execute("INSERT INTO meta VALUES ('current',?)", (s['runId'],))

    @contextmanager
    def connect(self):
        # A single backend owns live notifications; serialize remote transactions.
        with self.database_lock:
            db = (TursoConnection(self.turso_url, self.turso_token) if self.turso_url
                  else sqlite3.connect(self.path, timeout=20))
            try:
                if not self.turso_url:
                    db.execute('PRAGMA busy_timeout=20000')
                yield db
                db.commit()
            except Exception:
                self.cached_current = None
                try:
                    db.rollback()
                except Exception:
                    pass  # Preserve the original error, including uncertain commits.
                raise
            finally:
                db.close()

    @staticmethod
    def execute_group(db, statements):
        if hasattr(db, 'execute_group'):
            return db.execute_group(statements)
        return [db.execute(sql, parameters) for sql, parameters in statements]

    def encode_state(self, state):
        raw = json.dumps(state, separators=(',', ':'))
        if not self.turso_url:
            return raw
        # Keep the whole audit history, but avoid transferring megabytes of
        # repeated bag snapshots to/from the database for every station save.
        packed = base64.b64encode(zlib.compress(raw.encode(), 1)).decode('ascii')
        return json.dumps({'trailmixStorage':'zlib-v1', 'payload':packed}, separators=(',', ':'))

    @staticmethod
    def decode_state(row):
        if not row:
            raise Problem('Run not found.')
        state = json.loads(row[0])
        if state.get('trailmixStorage') == 'zlib-v1':
            state = json.loads(zlib.decompress(base64.b64decode(state['payload'], validate=True)))
        # Existing databases need no manual conversion; records remain intact.
        state.setdefault('people', [])
        state.setdefault('messages', [])
        state.setdefault('devices', [])
        state.setdefault('deliveries', [])
        state.setdefault('configs', [])
        return state

    @staticmethod
    def load(db, run=None):
        if run:
            row = db.execute('SELECT state FROM runs WHERE id=?', (run,)).fetchone()
        else:
            row = db.execute("SELECT state FROM runs WHERE id=(SELECT value FROM meta WHERE key='current')").fetchone()
        return Store.decode_state(row)

    def state(self, run=None, *, live=False):
        # Project before copying: screens do not need historical bag snapshots.
        with self.database_lock:
            cached = self.turso_url and not run and self.cached_current is not None
            if cached:
                source = self.cached_current
            else:
                with self.connect() as db:
                    source = self.load(db, run)
                if self.turso_url and not run:
                    self.cached_current = source
            state = (copy.deepcopy(live_state(source)) if live else
                     copy.deepcopy(source) if self.turso_url and not run else source)
        if not run:
            self.attach_presence(state)
        return state

    def publish_snapshot(self, state, live):
        # HTTP callers receive an independent projected copy. The full decoded
        # state can become the private cache without cloning the audit again.
        self.cached_current = state if live else copy.deepcopy(state)
        response = copy.deepcopy(live_state(state)) if live else state
        self.attach_presence(response)
        return response

    def attach_presence(self, state):
        with self.presence_lock:
            for device in state['devices']:
                live = self.presence.get(device['id'], {})
                device['lastSeen'] = live.get('seen', device['createdAt'])
                device['online'] = bool(live) and NOW()-device['lastSeen'] < 45000
                device['observedMode'] = live.get('mode', '')
                device['observedOperator'] = live.get('operator', '')

    def device_hello(self, req, *, live=False):
        did = req.get('deviceId')
        if not isinstance(did, str) or not 8 <= len(did) <= 100 or not all(c.isalnum() or c in '-_' for c in did):
            raise Problem('Invalid device identifier.')
        state = self.state(live=live)
        if not any(d['id'] == did for d in state['devices']):
            self.command(dict(action='deviceRegister', args={'device':did}, operator='Device setup',
                              deviceId=did, requestId=str(uuid.uuid4()), runId=state['runId'], revision=state['revision']), live=live)
        with self.presence_lock:
            live = self.presence.setdefault(did, {})
            live.update(seen=NOW(), mode=str(req.get('mode',''))[:100], operator=str(req.get('operator',''))[:100])
        self.notify_change()
        return self.state(live=live)

    def notify_change(self):
        with self.changed:
            self.serial += 1
            self.changed.notify_all()

    def touch_device(self, did):
        with self.presence_lock:
            if did in self.presence:
                self.presence[did]['seen'] = NOW()


    def runs(self):
        with self.connect() as db:
            rows = db.execute('SELECT state,archived FROM runs ORDER BY rowid DESC').fetchall()
        return [dict(id=s['runId'], name=s['batch'], started=s['started'], archived=bool(a),
                     bags=len(s['bags']), events=len(s['events'])) for raw, a in rows for s in [self.decode_state((raw,))]]

    def command(self, req, *, live=False):
        # Keep commit, snapshot publication, and change notification in order.
        with self.database_lock:
            response = self._command(req, live=live)
            return response

    def _command(self, req, *, live=False):
        if not isinstance(req, dict):
            raise Problem('Invalid command.')
        operator = str(req.get('operator', '')).strip()
        action = req.get('action')
        args = req.get('args', {})
        request_id = req.get('requestId')
        if not operator or len(operator) > 100:
            raise Problem('Enter your operator name (100 characters or fewer).')
        if not isinstance(action, str) or not isinstance(args, dict) or not isinstance(request_id, str) or not 8 <= len(request_id) <= 100:
            raise Problem('Invalid command or request ID.')
        digest = hashlib.sha256(json.dumps(req, sort_keys=True).encode()).hexdigest()
        with self.connect() as db:
            _, prior_rows, state_rows = self.execute_group(db, [
                ('BEGIN IMMEDIATE', ()),
                ('SELECT digest FROM requests WHERE id=?', (request_id,)),
                ("SELECT state FROM runs WHERE id=(SELECT value FROM meta WHERE key='current')", ())])
            prior = prior_rows.fetchone()
            s = self.decode_state(state_rows.fetchone())
            if prior:
                if not hmac.compare_digest(prior[0], digest):
                    raise Problem('Request ID already used for a different action.')
                response = dict(state=s, duplicate=True)
                db.commit()
                response['state'] = self.publish_snapshot(s, live)
                self.notify_change()  # The original commit may have lost its response.
                return response
            if req.get('runId') != s['runId']:
                raise Problem('The active run changed. Refresh before recording an action.')
            revision = req.get('revision')
            if isinstance(revision, bool) or not isinstance(revision, int) or not 0 <= revision <= s['revision']:
                raise Problem('Invalid run revision. Refresh and retry.')
            queued = req.get('queued') is True and action in QUEUED_ACTIONS
            if queued and (action in ('move', 'receive') or action=='routePickup' and args.get('source')!='labels') and not isinstance(args.get('expectedBagIds'), list):
                raise Problem('Queued delivery requires the exact bags selected at pickup or receipt.')
            if queued and action == 'fill' and not isinstance(args.get('mixBatch'), str):
                raise Problem('Queued fill requires the recorded mixing batch.')
            if revision != s['revision'] and action not in STAFF_ACTIONS and action != 'safetyReport' and not queued:
                intervening = [e for e in s['events'] if e.get('revision', 0) > revision]
                if any(e.get('action') not in STAFF_ACTIONS for e in intervening):
                    raise Problem('Another station updated the run. The latest data has been loaded; review and retry.')
            old = dict(runId=s['runId'], bags=copy.deepcopy(s['bags']), people=copy.deepcopy(s['people']))
            when = NOW()
            result = self.apply(s, action, args, operator, when)
            new = None
            if action == 'newRun':
                new = fresh()
                new['batch'] = args['name'].strip()
                choice=args.get('configId','current')
                if choice=='current':
                    new['stations'] = [dict(id=st['id'], empty=0, batch='') for st in s['stations']]
                    new['people'] = copy.deepcopy(s['people'])
                    new['devices'] = copy.deepcopy(s['devices'])
                else:
                    definition=next(c for c in s['configs'] if c['id']==choice)['definition']
                    new['stations']=[dict(id=st['id'],empty=0,batch='') for st in definition['stations']]
                    new['people']=copy.deepcopy(definition['people'])
                    for person in new['people']:
                        previous=next((p for p in s['people'] if p['id']==person['id']),{})
                        person.update(status='available',updatedAt=when,updatedBy=operator,version=max(person.get('version',0),previous.get('version',0))+1)
                    new['devices']=copy.deepcopy(definition['devices'])
                    for device in new['devices']:
                        previous=next((d for d in s['devices'] if d['id']==device['id']),{})
                        device.update(version=max(device.get('version',0),previous.get('version',0))+1,updatedAt=when,updatedBy=operator)
                    for device in s['devices']:
                        if not any(d['id']==device['id'] for d in new['devices']):
                            d=copy.deepcopy(device);d.update(personId='',mode='dashboard',version=d.get('version',0)+1);new['devices'].append(d)
                new['configs']=copy.deepcopy(s['configs'])
                result['nextRunId'] = new['runId']
            s['revision'] += 1
            payload = {k: v for k, v in args.items() if k != 'safetyPin'}
            event = dict(id=str(uuid.uuid4()), at=when, operator=operator,
                         requestId=request_id, recordedAt=req.get('recordedAt'), recordedStation=req.get('recordedStation'), type=result.pop('type', action),
                         action=action, qty=result.get('qty', len(result.get('bags', []))),
                         bags=result.get('bags', []), detail=result.get('detail', ''),
                         data=payload, changes=result, revision=s['revision'], runId=s['runId'])
            person = next((p for p in old['people'] if p['name'].casefold() == operator.casefold()), None)
            event['personId'] = person['id'] if person else None
            event['assignments'] = copy.deepcopy(person['assignments']) if person else []
            device = next((d for d in s['devices'] if d['id'] == req.get('deviceId')), None)
            event['deviceId'] = device['id'] if device else None
            event['deviceName'] = device['name'] if device else None
            # Keep per-bag before/after states for reconstructing queues and cycle times.
            affected = set(event['bags'])
            event['before'] = [b for b in old['bags'] if b['id'] in affected]
            event['after'] = [b for b in s['bags'] if b['id'] in affected]
            s['events'].append(event)
            writes = [
                ('INSERT INTO events(id,run_id,at,operator,action,payload,result) VALUES (?,?,?,?,?,?,?)',
                 (event['id'], s['runId'], when, operator, action, json.dumps(payload), json.dumps(event))),
                ('UPDATE runs SET state=? WHERE id=?', (self.encode_state(s), s['runId']))]
            if action == 'newRun':
                writes.extend([
                    ('UPDATE runs SET archived=1 WHERE id=?', (s['runId'],)),
                    ('INSERT INTO runs(id,state) VALUES (?,?)', (new['runId'], self.encode_state(new))),
                    ("UPDATE meta SET value=? WHERE key='current'", (new['runId'],))])
                s = new
            writes.append(('INSERT INTO requests VALUES (?,?,?)', (request_id, digest, old['runId'])))
            if action=='routePlanUpgrade':
                writes.append(("INSERT INTO meta(key,value) VALUES ('direct_sealing_v4','done') ON CONFLICT(key) DO NOTHING",()))
            if action=='workflowUpgrade':
                writes.append(("INSERT INTO meta(key,value) VALUES ('delivery_limits_v3','done') ON CONFLICT(key) DO NOTHING",()))
            if action=='rosterImport':
                writes.append(("INSERT INTO meta(key,value) VALUES ('class_roster_v1','done') ON CONFLICT(key) DO NOTHING",()))
            self.execute_group(db, writes)
            response = dict(state=s, event=event)
        response['state'] = self.publish_snapshot(response['state'], live)
        if live:
            response['event'] = copy.deepcopy(live_event(event))
        self.notify_change()
        return response

    def upgrade_workflow(self):
        with self.connect() as db:
            if db.execute("SELECT value FROM meta WHERE key='delivery_limits_v3'").fetchone():
                return
        state=self.state()
        return self.command(dict(action='workflowUpgrade',args={},operator='Workflow update',
            requestId='delivery-limits-v3',runId=state['runId'],revision=state['revision']))

    def upgrade_route_plan(self):
        with self.connect() as db:
            if db.execute("SELECT value FROM meta WHERE key='direct_sealing_v4'").fetchone():return
        state=self.state()
        return self.command(dict(action='routePlanUpgrade',args={},operator='Workflow update',
            requestId='direct-sealing-v4',runId=state['runId'],revision=state['revision']))

    def seed_class_roster(self):
        with self.connect() as db:
            if db.execute("SELECT value FROM meta WHERE key='class_roster_v1'").fetchone():
                return
        state = self.state()
        return self.command(dict(action='rosterImport',args={},operator='Roster setup',
            requestId='class-roster-v1',runId=state['runId'],revision=state['revision']))

    def commands(self, requests, *, live=False):
        """Commit up to fifty queued station actions in three remote round trips.

        Each action keeps its own ID and audit event. A failure rolls back the
        entire batch; a lost response can be retried without repeating actions.
        """
        started, cpu_started = time.monotonic(), time.thread_time()
        timings = {}
        if not isinstance(requests, list) or not 1 <= len(requests) <= 50:
            raise Problem('Send between 1 and 50 station actions.')
        ids = []
        digests = []
        for req in requests:
            if not isinstance(req, dict) or req.get('action') not in QUEUED_ACTIONS or req.get('queued') is not True:
                raise Problem('Batches accept queued station actions only.')
            rid, operator = req.get('requestId'), req.get('operator')
            if not isinstance(rid, str) or not 8 <= len(rid) <= 100 or rid in ids:
                raise Problem('Use distinct valid action IDs in a batch.')
            if not isinstance(operator, str) or not operator.strip() or len(operator.strip()) > 100 or not isinstance(req.get('args'), dict):
                raise Problem('Enter an operator name and valid action arguments.')
            ids.append(rid)
            digests.append(hashlib.sha256(json.dumps(req, sort_keys=True).encode()).hexdigest())
        with self.database_lock:
            timings['lock_seconds'] = time.monotonic()-started
            with self.connect() as db:
                phase = time.monotonic()
                _, prior_rows, state_rows = self.execute_group(db, [
                    ('BEGIN IMMEDIATE', ()),
                    ('SELECT id,digest FROM requests WHERE id IN ('+','.join('?' for _ in ids)+')', tuple(ids)),
                    ("SELECT state FROM runs WHERE id=(SELECT value FROM meta WHERE key='current')", ())])
                timings['read_seconds'] = time.monotonic()-phase
                phase = time.monotonic()
                prior = dict(prior_rows.fetchall())
                state = self.decode_state(state_rows.fetchone())
                timings['decode_seconds'] = time.monotonic()-phase
                phase = time.monotonic()
                results, writes, remap = [], [], {}
                event_by_request = {e.get('requestId'):e for e in state['events'] if e.get('requestId')}
                for event in state['events']:
                    if event.get('requestId') and event['action'] in ('fill','labelPickup','routePickup'):
                        created = event['bags'][0] if event['action']=='fill' else event['changes']['movementId'] if event['action']=='labelPickup' else event['changes']['deliveryIds'][0]
                        remap['pending-'+event['requestId']] = created
                        if event['action']=='routePickup' and event['changes'].get('movementId') is not None:
                            remap['pending-move-'+event['requestId']] = event['changes']['movementId']
                for req, digest in zip(requests, digests):
                    rid, action = req['requestId'], req['action']
                    if rid in prior:
                        if not hmac.compare_digest(prior[rid], digest):
                            raise Problem('Request ID already used for a different action.')
                        results.append(dict(requestId=rid, duplicate=True, event=event_by_request.get(rid)))
                        continue
                    if req.get('runId') != state['runId']:
                        raise Problem('The active run changed. Review held station entries.')
                    revision = req.get('revision')
                    if isinstance(revision,bool) or not isinstance(revision,int) or not 0 <= revision <= state['revision']:
                        raise Problem('Invalid run revision.')
                    args = copy.deepcopy(req['args'])
                    for key in ('bag','movement','delivery'):
                        if key in args:args[key] = remap.get(args[key],args[key])
                    if action in ('move','receive') or action=='routePickup' and args.get('source')!='labels':
                        if not isinstance(args.get('expectedBagIds'),list):
                            raise Problem('Queued delivery requires the exact selected bags.')
                        args['expectedBagIds'] = [remap.get(value,value) for value in args['expectedBagIds']]
                    if action=='fill' and not isinstance(args.get('mixBatch'),str):
                        raise Problem('Queued fill requires the recorded mixing batch.')
                    before_people = state['people']
                    selected = set(args.get('expectedBagIds',[]))
                    if 'bag' in args:selected.add(args['bag'])
                    if action=='routeDeliver':
                        package=next((d for d in state['deliveries'] if d['id']==args.get('delivery')),None)
                        if package:selected.update(package.get('remainingBagIds',[]))
                    before_bags = copy.deepcopy([b for b in state['bags'] if b['id'] in selected])
                    when = NOW()
                    operator = req['operator'].strip()
                    result = self.apply(state, action, args, operator, when)
                    state['revision'] += 1
                    payload = {k:v for k,v in args.items() if k!='safetyPin'}
                    event = dict(id=str(uuid.uuid4()), at=when, operator=operator, requestId=rid,
                        recordedAt=req.get('recordedAt'), recordedStation=req.get('recordedStation'),
                        type=result.pop('type',action), action=action, qty=result.get('qty',len(result.get('bags',[]))),
                        bags=result.get('bags',[]), detail=result.get('detail',''), data=payload,
                        changes=result, revision=state['revision'], runId=state['runId'])
                    person = next((p for p in before_people if p['name'].casefold()==operator.casefold()),None)
                    device = next((d for d in state['devices'] if d['id']==req.get('deviceId')),None)
                    event.update(personId=person['id'] if person else None,
                        assignments=copy.deepcopy(person['assignments']) if person else [],
                        deviceId=device['id'] if device else None,deviceName=device['name'] if device else None)
                    affected = set(event['bags'])
                    event['before'] = [b for b in before_bags if b['id'] in affected]
                    event['after'] = copy.deepcopy([b for b in state['bags'] if b['id'] in affected])
                    state['events'].append(event)
                    writes.extend([
                        ('INSERT INTO events(id,run_id,at,operator,action,payload,result) VALUES (?,?,?,?,?,?,?)',
                         (event['id'],state['runId'],when,operator,action,json.dumps(payload),json.dumps(event))),
                        ('INSERT INTO requests VALUES (?,?,?)',(rid,digest,state['runId']))])
                    results.append(dict(requestId=rid,duplicate=False,event=event))
                    if action in ('fill','labelPickup','routePickup'):
                        remap['pending-'+rid] = (event['bags'][0] if action=='fill' else event['changes']['movementId'] if action=='labelPickup' else event['changes']['deliveryIds'][0])
                        if action=='routePickup' and event['changes'].get('movementId') is not None:
                            remap['pending-move-'+rid] = event['changes']['movementId']
                timings['apply_seconds'] = time.monotonic()-phase
                phase = time.monotonic()
                if writes:
                    writes.append(('UPDATE runs SET state=? WHERE id=?',(self.encode_state(state),state['runId'])))
                timings['encode_seconds'] = time.monotonic()-phase
                phase = time.monotonic()
                if writes:
                    self.execute_group(db,writes)
                timings['write_seconds'] = time.monotonic()-phase
                phase = time.monotonic()
            timings['commit_seconds'] = time.monotonic()-phase
            phase = time.monotonic()
            state = self.publish_snapshot(state, live)
            if live:
                results = [dict(item, event=copy.deepcopy(live_event(item.get('event')))) for item in results]
            self.notify_change()
            timings['publish_seconds'] = time.monotonic()-phase
            timings['cpu_seconds'] = time.thread_time()-cpu_started
            return dict(state=state,results=results,timings=timings)

    def apply(self, s, action, a, operator, when):
        def fail(message):
            raise Problem(message)

        def safety():
            if not hmac.compare_digest(str(a.get('safetyPin', '')), self.safety_pin):
                fail('Safety Rover PIN required for this decision.')

        def note():
            text = str(a.get('note', '')).strip()
            if not text or len(text) > 4000:
                fail('Add a reason or note (up to 4,000 characters).')
            return text

        def num(key='qty'):
            v = a.get(key)
            if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 10000:
                fail('Enter a whole quantity from 1 to 10,000.')
            return v

        def queue(status, station=None):
            return sorted([b for b in s['bags'] if b['status'] == status and
                           (station is None or b['station'] == station)],
                          key=lambda b: (b.get('holdAt', b['updated']) if status == 'rework' else b['updated'], b['id']))

        def bag(statuses):
            found = next((b for b in s['bags'] if b['id'] == a.get('bag')), None)
            if not found or found['status'] not in statuses:
                fail('Bag is no longer in that queue. Refresh and retry.')
            return found

        def move(bs, status):
            for b in bs:
                b['status'], b['updated'] = status, when

        def take(status):
            bs = queue(status, a.get('station'))[:num()]
            if len(bs) != a['qty']:
                fail('Quantity exceeds available bags.')
            if 'expectedBagIds' in a and a['expectedBagIds'] != [b['id'] for b in bs]:
                fail('The selected bags changed. Review the held delivery before retrying.')
            return bs

        def result(kind, bs=(), detail='', **extra):
            return dict(type=kind, bags=[b['id'] for b in bs], detail=detail, **extra)

        def person_for(name):
            return next((p for p in s['people'] if not p.get('deletedAt') and p['name'].casefold()==name.casefold()),None)

        def shipment(source, station, bs=(), movement=None, legacy=False, who=None, at=None):
            owner=who or operator
            person=person_for(owner)
            d=dict(id=str(uuid.uuid4()),source=source,station=station,bagIds=[b['id'] for b in bs],
                remainingBagIds=[b['id'] for b in bs],qty=movement['qty'] if movement else len(bs),
                movementId=movement['id'] if movement else None,status='transit',pickedAt=at or when,
                pickedBy=owner,personId=person['id'] if person else None,receipts=[],legacy=legacy)
            s['deliveries'].append(d)
            return d

        def receipt(bs=(), movement=None):
            ids={b['id'] for b in bs}
            changed=[]
            for d in s['deliveries']:
                if d['status']!='transit':continue
                received=[id for id in d['remainingBagIds'] if id in ids]
                label_match=movement is not None and d.get('movementId')==movement
                if not received and not label_match:continue
                before=copy.deepcopy(d)
                d['remainingBagIds']=[id for id in d['remainingBagIds'] if id not in ids]
                d['receipts'].append(dict(at=when,operator=operator,qty=d['qty'] if label_match else len(received),bags=received))
                if label_match or not d['remainingBagIds']:
                    d.update(status='delivered',deliveredAt=when,deliveredBy=operator)
                changed.append(dict(before=before,after=copy.deepcopy(d)))
            return changed

        def measure(b, kind):
            value = a.get('mass')
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 or value > 10000:
                fail('Enter a positive, finite net mass.')
            unit = a.get('unit')
            if unit not in ('oz', 'g'):
                fail('Select oz or g.')
            oz = value if unit == 'oz' else value / OZ
            m = dict(id=str(uuid.uuid4()), oz=oz, entered=value, unit=unit, at=when, operator=operator, kind=kind,
                     minOz=s['limits']['min'],maxOz=s['limits']['max'],passed=s['limits']['min']-1e-9<=oz<=s['limits']['max']+1e-9)
            b['measurements'].append(m)
            return oz, s['limits']['min'] - 1e-9 <= oz <= s['limits']['max'] + 1e-9

        if s.get('safetyHold') and action not in ('workflowUpgrade','safetyRelease', 'note', 'safetyReport', 'spotcheck',
                'assignAtStation','configSave','routePlanUpgrade','personDelete', 'personRestore', 'rosterImport', 'personAddMany', 'personAdd', 'personUpdate', 'messagePost', 'messageAcknowledge', 'messageResolve',
                'deviceRegister', 'deviceUpdate'):
            fail('Production is on a safety hold. Ask the Safety Rover to release it.')
        roles = {'label', 'inspection', 'rework', 'sealing', 'final', 'stocker', 'firstMover',
                 'finalMover', 'mixing', 'safety', 'coordinator'} | {'delivery:'+source+':'+str(station) for source in DELIVERY_LEGS for station in ['*']+[st['id'] for st in s['stations']]} | {'fill:'+str(st['id']) for st in s['stations']}

        def assignments():
            values = a.get('assignments', [])
            if not isinstance(values, list) or any(not isinstance(v, str) or v not in roles for v in values):
                fail('Select valid station assignments.')
            return list(dict.fromkeys(values))

        if action == 'deviceRegister':
            did = a.get('device')
            if not isinstance(did, str) or not 8 <= len(did) <= 100 or not all(c.isalnum() or c in '-_' for c in did):
                fail('Invalid device identifier.')
            device = next((d for d in s['devices'] if d['id'] == did), None)
            if not device:
                device = dict(id=did, name='Device '+did[-6:], personId='', mode='',
                              version=0, createdAt=when, updatedAt=when, updatedBy=operator)
                s['devices'].append(device)
            return result('Device registered', detail=device['name'], deviceAfter=copy.deepcopy(device))
        if action == 'deviceUpdate':
            device = next((d for d in s['devices'] if d['id'] == a.get('device')), None)
            if not device:
                fail('Device not found. Open the app and connect it first.')
            if a.get('expectedVersion') != device['version']:
                fail('This device was updated elsewhere. Review its current assignment and retry.')
            name, person_id, mode = str(a.get('name','')).strip(), a.get('person',''), a.get('mode','')
            person = next((p for p in s['people'] if p['id'] == person_id and not p.get('deletedAt')), None)
            if not name or len(name)>80 or person_id and not person or mode not in roles|{'','dashboard','deliveryDisplay','delivery'}:
                fail('Choose a valid device name, person and station/view.')
            before = copy.deepcopy(device)
            device.update(name=name, personId=person_id, mode=mode, version=device['version']+1,
                          updatedAt=when, updatedBy=operator)
            extra = {}
            if person and mode in roles and mode not in ('stocker','firstMover','finalMover') and mode not in person['assignments']:
                extra['personBefore'] = copy.deepcopy(person)
                person['assignments'].append(mode)
                person.update(version=person.get('version',0)+1, updatedAt=when, updatedBy=operator)
                extra['personAfter'] = copy.deepcopy(person)
            return result('Device assignment updated', detail=name, deviceBefore=before,
                          deviceAfter=copy.deepcopy(device), **extra)

        if action=='routePlanUpgrade':
            changed=[];affected=[]
            for bag in s['bags']:
                latest=bag['measurements'][-1] if bag['measurements'] else None
                if bag['status']=='inspection' and latest and latest['kind']=='rework' and s['limits']['min']-1e-9<=latest['oz']<=s['limits']['max']+1e-9:
                    bag.update(status='ready',updated=when);affected.append(bag)
            for person in s['people']:
                before=copy.deepcopy(person)
                person['assignments']=list(dict.fromkeys('delivery:'+r.split(':')[1]+':*' if r.startswith('delivery:') and r.split(':')[1] not in ('labels','filled') else r for r in person['assignments']))
                if person!=before:
                    person.update(version=person.get('version',0)+1,updatedAt=when,updatedBy=operator)
                    changed.append(dict(before=before,after=copy.deepcopy(person)))
            for d in s['deliveries']:
                if d['status']=='transit' and d['source'] not in ('labels','filled'):d['station']=None
            return result('Rework route updated',affected,detail='Cleared rework goes directly to Sealing; downstream legs are shared',peopleUpdated=changed)
        if action in ('assignAtStation','configSave'):
            coordinator=person_for(operator)
            if not coordinator or not any(r in coordinator['assignments'] for r in ('coordinator','safety')):
                fail('Select a coordinator or Safety operator to manage station assignments and saved configurations.')
        if action=='assignAtStation':
            role=a.get('stationRole')
            if role not in {'label','inspection','rework','sealing','final'}|{'fill:'+str(st['id']) for st in s['stations']} and not (role in roles and role.startswith('delivery:')):
                fail('Choose a production station.')
            person=next((p for p in s['people'] if p['id']==a.get('person') and not p.get('deletedAt')),None)
            if not person:fail('Choose an active operator.')
            if a.get('expectedVersion')!=person.get('version',0):fail('This operator changed elsewhere. Review the current assignment and retry.')
            changed=[]
            targets=[person]+([p for p in s['people'] if p['id']!=person['id'] and role in p['assignments']] if a.get('replace') is True else [])
            for p in targets:
                before=copy.deepcopy(p)
                if p['id']==person['id']:
                    if role not in p['assignments']:p['assignments'].append(role)
                    p['status']='working'
                else:p['assignments']=[r for r in p['assignments'] if r!=role]
                p.update(version=p.get('version',0)+1,updatedAt=when,updatedBy=operator)
                changed.append(dict(before=before,after=copy.deepcopy(p)))
            devices=[]
            if a.get('device'):
                device=next((d for d in s['devices'] if d['id']==a['device']),None)
                if not device:fail('Device not found. Open the app on it first.')
                before=copy.deepcopy(device)
                device.update(personId=person['id'],mode='delivery' if role.startswith('delivery:') else role,version=device['version']+1,updatedAt=when,updatedBy=operator)
                devices.append(dict(before=before,after=copy.deepcopy(device)))
            return result('Station operator assigned',detail=person['name']+' → '+role,peopleUpdated=changed,devicesUpdated=devices)
        if action=='configSave':
            name=str(a.get('name','')).strip()
            if not name or len(name)>100:fail('Enter a configuration name up to 100 characters.')
            config=next((c for c in s['configs'] if c['name'].casefold()==name.casefold()),None)
            if config and a.get('expectedVersion')!=config['version']:fail('This saved configuration changed. Review it before overwriting.')
            if not config and len(s['configs'])>=20:fail('Up to 20 named configurations can be saved.')
            before=copy.deepcopy(config)
            definition=dict(stations=[dict(id=st['id']) for st in s['stations']],
                people=copy.deepcopy([p for p in s['people'] if not p.get('deletedAt')]),
                devices=copy.deepcopy(s['devices']))
            if not config:
                config=dict(id=str(uuid.uuid4()),name=name,createdAt=when,version=0)
                s['configs'].append(config)
            else:config['version']+=1
            config.update(name=name,definition=definition,updatedAt=when,updatedBy=operator)
            return result('Configuration saved',detail=name,configBefore=before,configAfter=copy.deepcopy(config))
        if action=='workflowUpgrade':
            old_limits=copy.deepcopy(s['limits'])
            affected=[]
            for b in s['bags']:
                for m in b['measurements']:
                    m.setdefault('minOz',old_limits['min']);m.setdefault('maxOz',old_limits['max'])
                    m.setdefault('passed',m['minOz']-1e-9<=m['oz']<=m['maxOz']+1e-9)
                inspection=next((m for m in reversed(b['measurements']) if m['kind']=='inspection'),None)
                if b['status']=='ready' and (not inspection or not 2-1e-9<=inspection['oz']<=2.25+1e-9):
                    b.update(status='hold',holdAt=when,updated=when);affected.append(b)
                latest=next((m for m in reversed(b['measurements']) if m['kind']=='rework'),None)
                if b['status']=='returnReady' and (not latest or not 2-1e-9<=latest['oz']<=2.25+1e-9):
                    b.update(status='rework',updated=when);affected.append(b)
            s['limits']=dict(min=2.0,max=2.25,confirmed=True)
            tracked={id for d in s['deliveries'] if d['status']=='transit' for id in d['remainingBagIds']}
            for source,leg in DELIVERY_LEGS.items():
                if source=='labels':continue
                groups={}
                for b in s['bags']:
                    if b['status']!=leg[3] or b['id'] in tracked:continue
                    event=next((e for e in reversed(s['events']) if b['id'] in e['bags'] and e['action']=='move'),None)
                    key=(b['station'],event['id'] if event else '')
                    if key not in groups:groups[key]=[[],event]
                    groups[key][0].append(b)
                for (station,_),(bs,event) in groups.items():
                    shipment(source,station,bs,legacy=True,who=event['operator'] if event else 'Previous pickup',at=event['at'] if event else when)
            tracked_moves={d.get('movementId') for d in s['deliveries'] if d['status']=='transit'}
            for m in s['moves']:
                if not m['received'] and m['id'] not in tracked_moves:
                    shipment('labels',m['station'],movement=m,legacy=True,who=m['operator'],at=m['at'])
            return result('Delivery routes and weight limits updated',affected,detail='2.00–2.25 oz net',previousLimits=old_limits,limits=copy.deepcopy(s['limits']),deliveries=copy.deepcopy(s['deliveries']))
        if action=='routePickup':
            source,station=a.get('source'),a.get('station')
            person=person_for(operator)
            if source not in ('labels','filled'):
                station=None;a=dict(a,station=None)
            if not route_allowed(person,source,station) or source in ('labels','filled') and not any(st['id']==station for st in s['stations']):
                fail('This delivery leg and filling station are not assigned to you.')
            if source=='labels':
                response=self.apply(s,'labelPickup',a,operator,when)
            else:
                response=self.apply(s,'move',a,operator,when)
            response['type']='Route pickup'
            return response
        if action=='routeDeliver':
            d=next((d for d in s['deliveries'] if d['id']==a.get('delivery')),None)
            person=person_for(operator)
            if not d or d['status']!='transit':fail('This package is already delivered or unavailable.')
            owned=(person and d.get('personId')==person['id']) or d['pickedBy'].casefold()==operator.casefold()
            if not owned and not (d.get('legacy') and route_allowed(person,d['source'],d['station'])):
                fail('This package was picked up by a different operator.')
            if d['source']=='labels':
                response=self.apply(s,'receiveLabels',{'movement':d['movementId']},operator,when)
                response['type']='Route delivered';return response
            source=DELIVERY_LEGS[d['source']][3]
            targets=d['remainingBagIds']
            bs=[next((b for b in s['bags'] if b['id']==id),None) for id in targets]
            if not bs or any(not b or b['status']!=source for b in bs):
                fail('Package contents changed. Review this delivery before proceeding.')
            destinations=dict(inspectionTransit='inspection',reworkTransit='rework',returnTransit='sealing',sealTransit='sealing',finalTransit='final')
            move(bs,destinations[source])
            changes=receipt(bs)
            return result('Route delivered',bs,detail=d['source']+' delivered',deliveryReceipts=changes,qty=len(bs))
        if action == 'rosterImport':
            added, updated = [], []
            for name in CLASS_ROSTER:
                matches = [p for p in s['people'] if p['name'].split()[0].casefold()==name.casefold()]
                if len(matches)>1:
                    fail('Multiple people match '+name+'. Rename the duplicates before importing.')
                roles_for_person = ['safety'] if name.casefold() in SAFETY_ROSTER else []
                if matches:
                    person = matches[0]
                    if person.get('deletedAt'):
                        continue
                    before = copy.deepcopy(person)
                    person.update(assignments=roles_for_person, updatedAt=when,updatedBy=operator,
                                  version=person.get('version',0)+1)
                    updated.append(dict(before=before,after=copy.deepcopy(person)))
                else:
                    person = dict(id=str(uuid.uuid4()),name=name,assignments=roles_for_person,
                        status='available',createdAt=when,updatedAt=when,updatedBy=operator,version=0)
                    s['people'].append(person)
                    added.append(copy.deepcopy(person))
            return result('Class roster prepared',qty=len(added),detail='Lucas, Blake, Charlie: Safety; other class members unassigned',
                          peopleAdded=added,peopleUpdated=updated)
        if action in ('personDelete','personRestore'):
            person = next((p for p in s['people'] if p['id']==a.get('person')),None)
            if not person:
                fail('Person not found.')
            before = copy.deepcopy(person)
            devices = []
            if action=='personDelete':
                if person.get('deletedAt'):fail('Operator is already deleted.')
                person.update(deletedAt=when,deletedBy=operator,status='off')
                for d in s['devices']:
                    if d.get('personId')==person['id']:
                        previous=copy.deepcopy(d)
                        d.update(personId='',version=d['version']+1,updatedAt=when,updatedBy=operator)
                        devices.append(dict(before=previous,after=copy.deepcopy(d)))
            else:
                if not person.get('deletedAt'):fail('Operator is already active.')
                person.pop('deletedAt',None)
                person.pop('deletedBy',None)
                person['status']='available'
            person.update(version=person.get('version',0)+1,updatedAt=when,updatedBy=operator)
            return result('Operator deleted' if action=='personDelete' else 'Operator restored',
                          detail=person['name'],personBefore=before,personAfter=copy.deepcopy(person),devicesUpdated=devices)
        if action in ('personAdd', 'personAddMany'):
            names = a.get('names') if action == 'personAddMany' else [a.get('name', '')]
            if not isinstance(names, list) or not 1 <= len(names) <= 100:
                fail('Enter between 1 and 100 operator names.')
            if any(not isinstance(name, str) or not name.strip() or len(name.strip()) > 100 for name in names):
                fail('Enter a person name, up to 100 characters.')
            names = [name.strip() for name in names]
            known = {p['name'].casefold() for p in s['people']}
            for name in names:
                if name.casefold() in known:
                    fail('That name is already in the roster. Use a distinct name for each person.')
                known.add(name.casefold())
            roles = assignments()
            people = [dict(id=str(uuid.uuid4()), name=name, assignments=copy.deepcopy(roles),
                           status='available', createdAt=when, updatedAt=when, updatedBy=operator, version=0)
                      for name in names]
            s['people'].extend(people)
            if action == 'personAdd':
                return result('Operator added', detail=names[0], personAfter=copy.deepcopy(people[0]))
            return result('Operators prepared', detail=', '.join(names), qty=len(people),
                          peopleAfter=copy.deepcopy(people))
        if action == 'personUpdate':
            person = next((p for p in s['people'] if p['id'] == a.get('person')), None)
            if not person:
                fail('Person not found.')
            if 'expectedVersion' in a and a['expectedVersion'] != person.get('version', 0):
                fail('This assignment was changed by someone else. Review the latest assignments and retry.')
            if person.get('deletedAt'):
                fail('Restore this operator before changing assignments.')
            name = str(a.get('name',person['name'])).strip()
            if not name or len(name)>100 or any(p['id']!=person['id'] and p['name'].casefold()==name.casefold() for p in s['people']):
                fail('Enter a distinct operator name up to 100 characters.')
            status = a.get('status')
            if status not in ('available', 'working', 'break', 'off'):
                fail('Select a valid operator status.')
            before = copy.deepcopy(person)
            person.update(name=name, assignments=assignments(), status=status, updatedAt=when, updatedBy=operator,
                          version=person.get('version', 0)+1)
            return result('Operator assignments updated', detail=person['name'],
                          personBefore=before, personAfter=copy.deepcopy(person))
        if action == 'messagePost':
            text = str(a.get('text', '')).strip()
            target_type, target = a.get('targetType'), a.get('target', '')
            if not text or len(text) > 1000:
                fail('Enter a message, up to 1,000 characters.')
            if target_type not in ('person', 'station', 'delivery', 'all'):
                fail('Choose a valid message audience.')
            if target_type == 'person' and not any(p['id'] == target for p in s['people']):
                fail('Message recipient not found.')
            if target_type == 'station' and target not in roles:
                fail('Message station not found.')
            priority = a.get('priority', 'normal')
            if priority not in ('normal', 'urgent'):
                fail('Choose normal or urgent priority.')
            message = dict(id=str(uuid.uuid4()), text=text, targetType=target_type,
                           target=target if target_type in ('person', 'station') else '',
                           priority=priority, createdAt=when, createdBy=operator,
                           acknowledgements=[], resolvedAt=None, resolvedBy=None)
            s['messages'].append(message)
            return result('Operator message posted', detail=text, messageAfter=copy.deepcopy(message))
        if action in ('messageAcknowledge', 'messageResolve'):
            message = next((m for m in s['messages'] if m['id'] == a.get('message')), None)
            if not message or message['resolvedAt']:
                fail('Message is already resolved or not found.')
            before = copy.deepcopy(message)
            if action == 'messageResolve':
                message.update(resolvedAt=when, resolvedBy=operator)
                return result('Operator message resolved', detail=message['text'],
                              messageBefore=before, messageAfter=copy.deepcopy(message))
            person = next((p for p in s['people'] if p['name'].casefold() == operator.casefold()), None)
            if not person:
                fail('Select your roster name before acknowledging a message.')
            eligible = (message['targetType'] == 'all' or
                        message['targetType'] == 'person' and message['target'] == person['id'] or
                        message['targetType'] == 'station' and message['target'] in person['assignments'] or
                        message['targetType'] == 'delivery' and any(r in ('stocker','firstMover','finalMover') or r.startswith('delivery:') for r in person['assignments']))
            if not eligible:
                fail('This message is addressed to a different operator or assignment.')
            if any(ack['personId'] == person['id'] for ack in message['acknowledgements']):
                fail('You already acknowledged this message.')
            message['acknowledgements'].append(dict(personId=person['id'], operator=operator, at=when))
            return result('Operator message acknowledged', detail=message['text'],
                          messageBefore=before, messageAfter=copy.deepcopy(message))
        if action == 'label':
            n = num()
            s['labels'] += n
            return result('Labeled bags added', qty=n)
        if action == 'labelPickup':
            n = num()
            station = next((x for x in s['stations'] if x['id'] == a.get('station')), None)
            if not station or n > s['labels']:
                fail('Choose a valid station and available quantity.')
            s['labels'] -= n
            m = dict(id=s['next'], station=station['id'], qty=n, at=when, operator=operator, received=False)
            s['next'] += 1
            s['moves'].append(m)
            d=shipment('labels',station['id'],movement=m)
            return result('Labeled bags picked up', qty=n, detail='To filling '+str(station['id']), movementId=m['id'],deliveryIds=[d['id']])
        if action == 'receiveLabels':
            m = next((x for x in s['moves'] if x['id'] == a.get('movement')), None)
            if not m or m['received']:
                fail('Pickup already received or not found.')
            st = next(x for x in s['stations'] if x['id'] == m['station'])
            st['empty'] += m['qty']
            m.update(received=True, receivedAt=when, receivedBy=operator)
            return result('Empty bags received', qty=m['qty'], detail='Filling '+str(st['id']), movementId=m['id'],deliveryReceipts=receipt(movement=m['id']))
        if action == 'assignBatch':
            st = next((x for x in s['stations'] if x['id'] == a.get('station')), None)
            batch = str(a.get('batch', '')).strip()
            if not st or not batch or len(batch) > 100:
                fail('Enter a valid mixing batch ID, up to 100 characters.')
            previous = st['batch']
            st['batch'] = batch
            return result('Mix batch assigned', detail='Filling '+str(st['id']), previous=previous, batch=batch)
        if action == 'fill':
            st = next((x for x in s['stations'] if x['id'] == a.get('station')), None)
            if not st or st['empty'] < 1:
                fail('Receive labeled empty bags first.')
            if not st['batch']:
                fail('Assign the mixing batch ID before filling.')
            if 'mixBatch' in a and a['mixBatch'] != st['batch']:
                fail('The mixing batch changed. Review the held fill before retrying.')
            st['empty'] -= 1
            b = dict(id=s['next'], station=st['id'], mixBatch=st['batch'], status='filled', created=when,
                     updated=when, measurements=[], firstFail=False, sealFails=0)
            s['next'] += 1
            s['bags'].append(b)
            return result('Bag filled', [b], 'Filling '+str(st['id']))
        if action == 'move':
            routes = dict(filled='inspectionTransit', hold='reworkTransit', returnReady='returnTransit',
                          ready='sealTransit', sealed='finalTransit')
            source = a.get('source')
            if source not in routes:
                fail('Invalid pickup route.')
            bs = take(source)
            if source in ('returnReady','ready'):
                kind='rework' if source=='returnReady' else 'inspection'
                for b in bs:
                    latest=next((m for m in reversed(b['measurements']) if m['kind']==kind or source=='ready' and m['kind']=='rework'),None)
                    if not latest or not s['limits']['min']-1e-9<=latest['oz']<=s['limits']['max']+1e-9:
                        fail(('Rework' if source=='returnReady' else 'First inspection')+' must be in spec before delivery.')
            move(bs, routes[source])
            deliveries=([shipment(source,station,[b for b in bs if b['station']==station]) for station in sorted({b['station'] for b in bs})] if source=='filled' else [shipment(source,None,bs)])
            return result('Transport pickup', bs, source+' → '+routes[source], source=source, destination=routes[source],deliveryIds=[d['id'] for d in deliveries])
        if action == 'receive':
            routes = dict(inspectionTransit='inspection', reworkTransit='rework', returnTransit='sealing',
                          sealTransit='sealing', finalTransit='final')
            source = a.get('source')
            if source not in routes:
                fail('Invalid receipt route.')
            bs = take(source)
            move(bs, routes[source])
            return result('Transport received', bs, source+' → '+routes[source], source=source, destination=routes[source],deliveryReceipts=receipt(bs))
        if action in ('weigh', 'clear'):
            b = bag(['inspection' if action == 'weigh' else 'rework'])
            if queue(b['status'])[0]['id'] != b['id']:
                fail('Process the oldest bag first.')
            if action == 'weigh' and not isinstance(a.get('labelOK'), bool):
                fail('Record the label check.')
            if action=='clear':
                value=a.get('mass');unit=a.get('unit')
                oz=value/OZ if unit=='g' and isinstance(value,(int,float)) else value
                if isinstance(value,bool) or not isinstance(oz,(int,float)) or not math.isfinite(oz) or unit not in ('oz','g') or not s['limits']['min']-1e-9<=oz<=s['limits']['max']+1e-9:
                    fail(f"Rework must be {s['limits']['min']:.2f}–{s['limits']['max']:.2f} oz net before delivery. Correct the mass and try again.")
            oz, passed = measure(b, 'inspection' if action == 'weigh' else 'rework')
            if action == 'weigh':
                inspections = [m for m in b['measurements'] if m['kind'] == 'inspection']
                if len(inspections) == 1:
                    b['firstFail'] = not passed
                    b['firstLabelFail'] = not a['labelOK']
                b['labelOK'] = a['labelOK']
                if not a['labelOK']:
                    move([b], 'discarded')
                    b['discardReason'] = 'First inspection label failure'
                    return result('Label failed · discarded', [b], b['discardReason'], massOz=oz)
                if passed:
                    move([b], 'ready')
                    return result('Weight passed', [b], f'{oz:.4f} oz net', massOz=oz)
                b['holdAt'] = when
                move([b], 'hold')
                return result('Weight failed', [b], 'Underweight → rework hold' if oz < s['limits']['min'] else 'Overweight → rework hold', massOz=oz)
            if passed:
                move([b], 'returnReady')
                return result('Rework cleared for sealing', [b], f'{oz:.4f} oz net', massOz=oz)
            return result('Rework still out of spec', [b], f'{oz:.4f} oz net', massOz=oz)
        if action in ('seal', 'failSeal'):
            b = bag(['sealing'])
            if action == 'seal':
                b['sealedAt'] = when
                move([b], 'sealed')
                return result('Seal passed', [b])
            b['sealFails'] += 1
            move([b], 'sealFailed')
            return result('Seal failed', [b], note())
        if action == 'finish':
            b = bag(['final'])
            checks = a.get('checks', {})
            if not isinstance(checks, dict) or not all(checks.get(k) is True for k in ('seal', 'label', 'leak', 'trappedProduct', 'contamination')):
                fail('Confirm all five final inspection checks.')
            b['completedAt'] = when
            move([b], 'done')
            return result('Final pass', [b], 'Seal, label, leaks, trapped product and contamination checked', checks=checks)
        if action == 'finalFail':
            b = bag(['final'])
            move([b], 'finalHold')
            return result('Final check failed', [b], note())
        if action == 'disposition':
            safety()
            b = bag(['qualityHold', 'sealFailed', 'finalHold'])
            reason = note()
            dest = a.get('destination')
            if dest == 'reseal':
                if b['status'] == 'qualityHold' or a.get('bagLabelOK') is not True:
                    fail('Reseal requires acceptable bag and label; weight holds cannot bypass inspection.')
                move([b], 'sealing')
            elif dest == 'reinspect':
                move([b], 'inspection')
            elif dest == 'discard':
                move([b], 'discarded')
                b['discardReason'] = reason
            elif dest == 'rebag':
                station = next(x for x in s['stations'] if x['id'] == b['station'])
                if station['empty'] < 1:
                    fail('Receive a labeled replacement bag at the original filling station first.')
                station['empty'] -= 1
                move([b], 'discarded')
                b['discardReason'] = 'Rebagged: '+reason
                new = dict(id=s['next'], station=b['station'], mixBatch=b['mixBatch'], status='filled',
                           created=when, updated=when, measurements=[], firstFail=False, sealFails=0, replaces=b['id'])
                s['next'] += 1
                b['replacedBy'] = new['id']
                s['bags'].append(new)
                return result('Safety disposition · rebag', [b, new], reason)
            else:
                fail('Choose a valid disposition.')
            return result('Safety disposition · '+dest, [b], reason)
        if action == 'spotcheck':
            safety()
            b = bag(['final', 'sealed', 'done', 'finalHold'])
            if action=='clear':
                value=a.get('mass');unit=a.get('unit')
                oz=value/OZ if unit=='g' and isinstance(value,(int,float)) else value
                if isinstance(value,bool) or not isinstance(oz,(int,float)) or not math.isfinite(oz) or unit not in ('oz','g') or not s['limits']['min']-1e-9<=oz<=s['limits']['max']+1e-9:
                    fail(f"Rework must be {s['limits']['min']:.2f}–{s['limits']['max']:.2f} oz net before delivery. Correct the mass and try again.")
            oz, passed = measure(b, 'spotcheck')
            if not passed:
                move([b], 'finalHold')
                b.pop('completedAt', None)
            return result('Weight spot check', [b], note(), massOz=oz, passed=passed)
        if action == 'safetyReport':
            s['safetyHold'] = True
            return result('Food safety issue reported · production held', detail=note())
        if action == 'safetyRelease':
            safety()
            s['safetyHold'] = False
            return result('Safety hold released', detail=note())
        if action == 'note':
            return result('Correction / production note', detail=note(), references=a.get('references', []))
        if action == 'settings':
            name = str(a.get('name', '')).strip()
            if not name or len(name) > 100:
                fail('Enter a run name, up to 100 characters.')
            s['batch'] = name
            return result('Run renamed', detail=name)
        if action == 'station':
            if len(s['stations']) >= 12:
                fail('Maximum 12 filling stations.')
            s['stations'].append(dict(id=len(s['stations'])+1, empty=0, batch=''))
            return result('Filling station added')
        if action == 'newRun':
            safety()
            name = str(a.get('name', '')).strip()
            if not name or len(name) > 100:
                fail('Enter the new run name, up to 100 characters.')
            if any(b['status'] not in ('done', 'discarded') for b in s['bags']) or s['labels'] or any(st['empty'] for st in s['stations']) or any(not m['received'] for m in s['moves']):
                fail('Account for all bags and empty inventory before archiving. Use a Safety Rover reconciliation.')
            choice=a.get('configId','current')
            if choice!='current' and not any(c['id']==choice for c in s['configs']):fail('Saved configuration not found.')
            return result('Run archived', detail='Next run: '+name,configuration=choice)
        if action == 'reconcile':
            safety()
            reason = note()
            bs = [b for b in s['bags'] if b['status'] not in ('done', 'discarded')]
            for b in bs:
                b['discardReason'] = 'End-of-run reconciliation: '+reason
            move(bs, 'discarded')
            inventory = dict(labels=s['labels'], stations=copy.deepcopy(s['stations']), inTransit=[m for m in s['moves'] if not m['received']])
            for d in s['deliveries']:
                if d['status']=='transit':d.update(status='cancelled',cancelledAt=when,cancelledBy=operator)
            s['labels'] = 0
            for st in s['stations']:
                st['empty'] = 0
            for m in s['moves']:
                if not m['received']:
                    m.update(received=True, reconciledAt=when, reconciliation=reason)
            return result('End-of-run reconciliation', bs, reason, remainingInventory=inventory)
        fail('Unknown action.')

    def export(self, kind, run=None):
        s = self.state(run)
        if kind == 'json':
            return 'application/json', json.dumps(s, indent=2).encode()
        rows = []
        if kind == 'events':
            rows = [['event_id', 'run_id', 'utc_time', 'unix_ms', 'operator', 'person_id', 'assigned_roles_json', 'device_id', 'device_name', 'action', 'quantity', 'bag_ids', 'detail', 'submitted_data_json', 'changes_json', 'before_json', 'after_json', 'request_id', 'device_recorded_ms', 'recorded_station']]
            for e in s['events']:
                rows.append([e['id'], s['runId'], time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(e['at']/1000)), e['at'], e['operator'], e.get('personId',''), json.dumps(e.get('assignments',[])), e.get('deviceId',''), e.get('deviceName',''), e['type'], e['qty'], '|'.join(map(str,e['bags'])), e['detail'], json.dumps(e['data']), json.dumps(e['changes']), json.dumps(e['before']), json.dumps(e['after']), e.get('requestId',''), e.get('recordedAt',''), e.get('recordedStation','')])
        elif kind == 'deliveries':
            rows=[['run_id','delivery_id','source','filling_station','quantity','picked_by','person_id','picked_ms','status','delivered_ms','delivered_by','bag_ids_json','remaining_bag_ids_json','receipts_json']]
            for d in s['deliveries']:
                rows.append([s['runId'],d['id'],d['source'],d['station'],d['qty'],d['pickedBy'],d.get('personId',''),d['pickedAt'],d['status'],d.get('deliveredAt',''),d.get('deliveredBy',''),json.dumps(d['bagIds']),json.dumps(d['remainingBagIds']),json.dumps(d['receipts'])])
        elif kind == 'people':
            rows = [['run_id','person_id','name','status','assigned_roles_json','updated_ms','last_action_ms','recorded_actions','bags_filled','inspection_readings','rework_readings','final_passes']]
            for p in s['people']:
                events = [e for e in s['events'] if e.get('personId') == p['id'] or e['operator'].casefold() == p['name'].casefold()]
                rows.append([s['runId'],p['id'],p['name'],p['status'],json.dumps(p['assignments']),p['updatedAt'],events[-1]['at'] if events else '',len(events),sum(e['qty'] for e in events if e['action']=='fill'),sum(e['qty'] for e in events if e['action']=='weigh'),sum(e['qty'] for e in events if e['action']=='clear'),sum(e['qty'] for e in events if e['action']=='finish')])
        elif kind == 'devices':
            rows = [['run_id','device_id','name','assigned_person_id','assigned_view','configured_ms','configured_by','last_seen_ms','last_reported_view','last_reported_operator']]
            for d in s['devices']:
                rows.append([s['runId'],d['id'],d['name'],d['personId'],d['mode'],d['updatedAt'],d['updatedBy'],d.get('lastSeen',''),d.get('observedMode',''),d.get('observedOperator','')])
        elif kind == 'messages':
            rows = [['run_id','message_id','audience_type','audience_id','text','priority','created_ms','created_by','acknowledgements_json','resolved_ms','resolved_by']]
            for m in s['messages']:
                rows.append([s['runId'],m['id'],m['targetType'],m['target'],m['text'],m['priority'],m['createdAt'],m['createdBy'],json.dumps(m['acknowledgements']),m['resolvedAt'] or '',m['resolvedBy'] or ''])
        elif kind == 'weights':
            rows = [['run_id','bag_id','filling_station','mix_batch','measurement_id','kind','utc_time','operator','entered_value','entered_unit','net_oz','net_g','in_spec']]
            for b in s['bags']:
                for m in b['measurements']:
                    rows.append([s['runId'],b['id'],b['station'],b['mixBatch'],m['id'],m['kind'],time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(m['at']/1000)),m['operator'],m['entered'],m['unit'],m['oz'],m['oz']*OZ,m.get('passed',m.get('minOz',s['limits']['min'])-1e-9<=m['oz']<=m.get('maxOz',s['limits']['max'])+1e-9)])
        elif kind == 'bags':
            rows = [['run_id','bag_id','filling_station','mix_batch','status','created_ms','completed_ms','cycle_seconds','first_weight_failed','seal_failures','replaces','replaced_by']]
            for b in s['bags']:
                rows.append([s['runId'],b['id'],b['station'],b['mixBatch'],b['status'],b['created'],b.get('completedAt',''),(b['completedAt']-b['created'])/1000 if b.get('completedAt') else '',b['firstFail'],b['sealFails'],b.get('replaces',''),b.get('replacedBy','')])
        else:
            raise Problem('Unknown export format.')
        out = io.StringIO(newline='')
        # Prevent operator-entered strings from becoming spreadsheet formulas.
        writer = csv.writer(out)
        writer.writerows([["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@','\t','\r')) else v for v in row] for row in rows])
        return 'text/csv; charset=utf-8', out.getvalue().encode('utf-8-sig')


def live_event(event):
    if event is None:
        return None
    return {key:value for key,value in event.items() if key not in ('before', 'after')}


def live_state(state):
    return dict(state, events=[live_event(event) for event in state['events']])


def live_response(body):
    if not isinstance(body, dict):
        return body
    if 'runId' in body and 'events' in body:
        return live_state(body)
    if 'state' not in body:
        return body
    result = dict(body, state=live_state(body['state']))
    if 'event' in body:
        result['event'] = live_event(body['event'])
    if 'results' in body:
        result['results'] = [dict(item, event=live_event(item.get('event'))) for item in body['results']]
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = 'TrailMix/1.0'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        # Do not log access codes, PINs, cookies, or request bodies.
        print('%s %s' % (self.log_date_time_string(), fmt % args))

    def respond(self, code, body, content='application/json', cookie=None, filename=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(live_response(body), separators=(',', ':')).encode()
        if isinstance(body, str):
            body = body.encode()
        try:
            self.send_response(code)
            self.send_header('Content-Type', content)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'same-origin')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            if cookie:
                self.send_header('Set-Cookie', cookie)
            if filename:
                self.send_header('Content-Disposition', 'attachment; filename="'+filename+'"')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            # A command may already be committed. A disconnected response must
            # never become a save failure or trigger a second HTTP response.
            self.close_connection = True


    def authorized(self):
        if not getattr(self.server, 'require_access_code', False):
            return True
        c = SimpleCookie()
        try:
            c.load(self.headers.get('Cookie', ''))
            token = c['trailmix'].value
        except Exception:
            return False
        return hmac.compare_digest(token, self.server.auth_cookie)

    def do_HEAD(self):
        # Hosting probes only need the page headers; never open an SSE stream.
        if urlparse(self.path).path in ('/', '/index.html'):
            self.respond(200, (ROOT/'trailmix.html').read_bytes(), 'text/html; charset=utf-8')
        else:
            self.respond(404, {'error':'Not found.'})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ('/', '/index.html'):
            self.respond(200, (ROOT/'trailmix.html').read_bytes(), 'text/html; charset=utf-8')
            return
        if not self.authorized():
            self.respond(401, {'error':'Enter the team access code to connect.'})
            return
        try:
            params = parse_qs(parsed.query)
            run = params.get('run', [None])[0]
            if path == '/api/stream':
                did = params.get('deviceId',[''])[0]
                with self.server.store.changed:
                    seen = self.server.store.serial
                initial_state = self.server.store.state(live=True)
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.send_header('Cache-Control','no-cache, no-transform')
                self.send_header('X-Accel-Buffering','no')
                self.send_header('Connection','keep-alive')
                self.end_headers()
                self.wfile.write(b'retry: 10000\n\n')
                def emit(kind, data):
                    self.wfile.write(('event: '+kind+'\ndata: '+json.dumps(live_response(data), separators=(',', ':'))+'\n\n').encode())
                    self.wfile.flush()
                try:
                    self.server.store.touch_device(did)
                    emit('state',initial_state)
                    while True:
                        with self.server.store.changed:
                            changed = self.server.store.changed.wait_for(lambda:self.server.store.serial!=seen,timeout=20)
                            seen = self.server.store.serial
                        self.server.store.touch_device(did)
                        if changed:
                            initial_state = self.server.store.state(live=True)
                            emit('state',initial_state)
                        else:
                            devices = copy.deepcopy(initial_state['devices'])
                            self.server.store.attach_presence({'devices':devices})
                            emit('ping',dict(at=NOW(), devices=devices))
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError,TimeoutError,RemoteDatabaseError):
                    self.close_connection = True
                return
            if path == '/api/state':
                self.server.store.touch_device(params.get('deviceId',[''])[0])
                self.respond(200, self.server.store.state(live=True))
            elif path == '/api/runs':
                self.respond(200, self.server.store.runs())
            elif path == '/api/export':
                kind = params.get('kind', ['events'])[0]
                mime, data = self.server.store.export(kind, run)
                self.respond(200, data, mime, filename='trailmix-'+kind+('.json' if kind=='json' else '.csv'))
            else:
                self.respond(404, {'error':'Not found.'})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True
        except RemoteDatabaseError:
            self.respond(503, {'error':'Database unavailable. Reconnect or retry the same save.', 'retryable':True})
        except Problem as e:
            self.respond(400, {'error':str(e)})

    def do_POST(self):
        # Custom header + JSON prevent cross-site form submissions (no CORS allowed).
        if self.headers.get('X-TrailMix') != '1' or self.headers.get('Content-Type','').split(';')[0] != 'application/json':
            self.close_connection = True  # Rejected body is intentionally unread.
            self.respond(403, {'error':'Invalid request origin or content type.'})
            return
        origin = self.headers.get('Origin')
        if origin and urlparse(origin).netloc != self.headers.get('Host'):
            self.close_connection = True
            self.respond(403, {'error':'Cross-origin writes are not allowed.'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 65536:
                raise Problem('Invalid request size.')
            req = json.loads(self.rfile.read(length))
            if not isinstance(req, dict):
                raise Problem('Invalid JSON object.')
            if self.path == '/api/login':
                if not getattr(self.server, 'require_access_code', False):
                    self.respond(200, {'ok':True})
                    return
                if time.time() < self.server.login_blocked_until:
                    self.respond(429, {'error':'Too many access-code attempts. Try again in a minute.'})
                    return
                if not hmac.compare_digest(str(req.get('code','')), self.server.access_code):
                    self.server.login_failures += 1
                    if self.server.login_failures >= 20:
                        self.server.login_blocked_until = time.time()+60
                        self.server.login_failures = 0
                    self.respond(401, {'error':'Incorrect team access code.'})
                    return
                cookie = 'trailmix='+self.server.auth_cookie+'; HttpOnly; SameSite=Strict; Path=/'
                if os.getenv('TRAILMIX_HTTPS') == '1':
                    cookie += '; Secure'
                self.respond(200, {'ok':True}, cookie=cookie)
                return
            if not self.authorized():
                self.respond(401, {'error':'Enter the team access code to connect.'})
                return
            if self.path == '/api/device/hello':
                self.respond(200, self.server.store.device_hello(req, live=True))
                return
            if self.path == '/api/actions':
                started = time.monotonic()
                result = self.server.store.commands(req.get('commands'), live=True)
                self.log_message('Batch saved: actions=%d save_seconds=%.3f',
                                 len(result['results']), time.monotonic()-started)
                self.log_message('Batch phases: %s', ' '.join(key+'='+format(value,'.3f') for key,value in result['timings'].items()))
                self.respond(200, result)
                return
            if self.path != '/api/action':
                self.respond(404, {'error':'Not found.'})
                return
            self.respond(200, self.server.store.command(req, live=True))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True
        except RemoteDatabaseError:
            self.respond(503, {'error':'Database unavailable or save not confirmed. Retry the same save.', 'retryable':True})
        except (Problem, ValueError, TypeError) as e:
            self.respond(409, {'error':str(e)})
        except Exception:
            import traceback
            traceback.print_exc()
            self.respond(500, {'error':'Save not confirmed. Retry the same save; do not repeat physical actions.', 'retryable':True})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=int(os.getenv('PORT', '8080')))
    parser.add_argument('--db', default=os.getenv('TRAILMIX_DB', str(ROOT/'data'/'trailmix.sqlite3')))
    args = parser.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    turso_url, turso_token = os.getenv('TURSO_DATABASE_URL'), os.getenv('TURSO_AUTH_TOKEN')
    if os.getenv('RENDER') and not turso_url:
        raise SystemExit('Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN before starting on Render. Local files are not durable on the free plan.')
    store = Store(args.db, os.getenv('TRAILMIX_SAFETY_PIN'),
                  turso_url=turso_url, turso_token=turso_token)
    if os.getenv('TRAILMIX_UPGRADE_WORKFLOW','1')=='1':
        store.upgrade_workflow()
        store.upgrade_route_plan()
    if os.getenv('TRAILMIX_SEED_ROSTER','1')=='1':
        store.seed_class_roster()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.store = store
    server.require_access_code = os.getenv('TRAILMIX_REQUIRE_ACCESS_CODE', '0') == '1'
    server.access_code = os.getenv('TRAILMIX_ACCESS_CODE') or ''
    if server.require_access_code and not server.access_code:
        raise SystemExit('Set TRAILMIX_ACCESS_CODE when TRAILMIX_REQUIRE_ACCESS_CODE=1.')
    server.auth_cookie = store.auth_cookie
    server.login_failures, server.login_blocked_until = 0, 0
    access_status = 'Team access code required (configured in environment)' if server.require_access_code else 'Team sign-in: not required'
    pin_status = store.safety_pin if store.pin_created else 'saved; unchanged across restarts (TRAILMIX_SAFETY_PIN can override it)'
    print(f'\nTrailMix is running at http://localhost:{args.port}\nOther devices: http://THIS-COMPUTERS-LAN-IP:{args.port}\n{access_status}\nSafety Rover PIN: {pin_status}\nDatabase: {'Turso (remote persistent storage)' if turso_url else Path(args.db).resolve()}\nUse Ctrl+C to stop.\n', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped. Records remain in the database.')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
