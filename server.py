#!/usr/bin/env python3
"""TrailMix: browser stations, SQLite persistence, append-only production records."""
import argparse
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
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from turso_db import TursoConnection, RemoteDatabaseError

ROOT = Path(__file__).resolve().parent
OZ = 28.349523125
NOW = lambda: int(time.time() * 1000)
QUEUED_ACTIONS = {'label', 'labelPickup', 'receiveLabels', 'fill', 'move', 'receive',
                  'weigh', 'clear', 'seal', 'failSeal', 'finish', 'finalFail'}
STAFF_ACTIONS = {'personAddMany', 'personAdd', 'personUpdate', 'messagePost', 'messageAcknowledge', 'messageResolve',
                 'deviceRegister', 'deviceUpdate'}


def fresh():
    return dict(version=2, revision=0, runId=str(uuid.uuid4()), batch='Trail mix · Run 01',
                started=NOW(), demo=False, limits=dict(min=1.5, max=1.75, confirmed=True),
                stations=[dict(id=i, empty=0, batch='') for i in range(1, 4)],
                labels=0, moves=[], bags=[], events=[], next=1, safetyHold=False,
                people=[], messages=[], devices=[])


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
                db.execute('INSERT INTO runs(id,state) VALUES (?,?)', (s['runId'], json.dumps(s)))
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

    @staticmethod
    def decode_state(row):
        if not row:
            raise Problem('Run not found.')
        state = json.loads(row[0])
        # Existing databases need no manual conversion; records remain intact.
        state.setdefault('people', [])
        state.setdefault('messages', [])
        state.setdefault('devices', [])
        return state

    @staticmethod
    def load(db, run=None):
        if run:
            row = db.execute('SELECT state FROM runs WHERE id=?', (run,)).fetchone()
        else:
            row = db.execute("SELECT state FROM runs WHERE id=(SELECT value FROM meta WHERE key='current')").fetchone()
        return Store.decode_state(row)

    def state(self, run=None):
        # One server owns writes and SSE. Share its committed snapshot across
        # screens rather than querying Turso separately for every subscriber.
        with self.database_lock:
            if self.turso_url and not run and self.cached_current is not None:
                state = copy.deepcopy(self.cached_current)
            else:
                with self.connect() as db:
                    state = self.load(db, run)
                if self.turso_url and not run:
                    self.cached_current = copy.deepcopy(state)
        if not run:
            self.attach_presence(state)
        return state

    def attach_presence(self, state):
        with self.presence_lock:
            for device in state['devices']:
                live = self.presence.get(device['id'], {})
                device['lastSeen'] = live.get('seen', device['createdAt'])
                device['online'] = bool(live) and NOW()-device['lastSeen'] < 45000
                device['observedMode'] = live.get('mode', '')
                device['observedOperator'] = live.get('operator', '')

    def device_hello(self, req):
        did = req.get('deviceId')
        if not isinstance(did, str) or not 8 <= len(did) <= 100 or not all(c.isalnum() or c in '-_' for c in did):
            raise Problem('Invalid device identifier.')
        state = self.state()
        if not any(d['id'] == did for d in state['devices']):
            self.command(dict(action='deviceRegister', args={'device':did}, operator='Device setup',
                              deviceId=did, requestId=str(uuid.uuid4()), runId=state['runId'], revision=state['revision']))
        with self.presence_lock:
            live = self.presence.setdefault(did, {})
            live.update(seen=NOW(), mode=str(req.get('mode',''))[:100], operator=str(req.get('operator',''))[:100])
        self.notify_change()
        return self.state()

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
                     bags=len(s['bags']), events=len(s['events'])) for raw, a in rows for s in [json.loads(raw)]]

    def command(self, req):
        # Keep commit, snapshot publication, and change notification in order.
        with self.database_lock:
            response = self._command(req)
            return response

    def _command(self, req):
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
                self.cached_current = copy.deepcopy(s)
                self.attach_presence(response['state'])
                self.notify_change()  # The original commit may have lost its response.
                return response
            if req.get('runId') != s['runId']:
                raise Problem('The active run changed. Refresh before recording an action.')
            revision = req.get('revision')
            if isinstance(revision, bool) or not isinstance(revision, int) or not 0 <= revision <= s['revision']:
                raise Problem('Invalid run revision. Refresh and retry.')
            queued = req.get('queued') is True and action in QUEUED_ACTIONS
            if queued and action in ('move', 'receive') and not isinstance(args.get('expectedBagIds'), list):
                raise Problem('Queued delivery requires the exact bags selected at pickup or receipt.')
            if queued and action == 'fill' and not isinstance(args.get('mixBatch'), str):
                raise Problem('Queued fill requires the recorded mixing batch.')
            if revision != s['revision'] and action not in STAFF_ACTIONS and action != 'safetyReport' and not queued:
                intervening = [e for e in s['events'] if e.get('revision', 0) > revision]
                if any(e.get('action') not in STAFF_ACTIONS for e in intervening):
                    raise Problem('Another station updated the run. The latest data has been loaded; review and retry.')
            old = copy.deepcopy(s)
            when = NOW()
            result = self.apply(s, action, args, operator, when)
            new = None
            if action == 'newRun':
                new = fresh()
                new['batch'] = args['name'].strip()
                new['stations'] = [dict(id=st['id'], empty=0, batch='') for st in s['stations']]
                new['people'] = copy.deepcopy(s['people'])
                new['devices'] = copy.deepcopy(s['devices'])
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
                ('UPDATE runs SET state=? WHERE id=?', (json.dumps(s), s['runId']))]
            if action == 'newRun':
                writes.extend([
                    ('UPDATE runs SET archived=1 WHERE id=?', (s['runId'],)),
                    ('INSERT INTO runs(id,state) VALUES (?,?)', (new['runId'], json.dumps(new))),
                    ("UPDATE meta SET value=? WHERE key='current'", (new['runId'],))])
                s = new
            writes.append(('INSERT INTO requests VALUES (?,?,?)', (request_id, digest, old['runId'])))
            self.execute_group(db, writes)
            response = dict(state=s, event=event)
        self.cached_current = copy.deepcopy(response['state'])
        self.attach_presence(response['state'])
        self.notify_change()
        return response

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

        def measure(b, kind):
            value = a.get('mass')
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 or value > 10000:
                fail('Enter a positive, finite net mass.')
            unit = a.get('unit')
            if unit not in ('oz', 'g'):
                fail('Select oz or g.')
            oz = value if unit == 'oz' else value / OZ
            m = dict(id=str(uuid.uuid4()), oz=oz, entered=value, unit=unit, at=when, operator=operator, kind=kind)
            b['measurements'].append(m)
            return oz, s['limits']['min'] - 1e-9 <= oz <= s['limits']['max'] + 1e-9

        if s.get('safetyHold') and action not in ('safetyRelease', 'note', 'safetyReport', 'spotcheck',
                'personAddMany', 'personAdd', 'personUpdate', 'messagePost', 'messageAcknowledge', 'messageResolve',
                'deviceRegister', 'deviceUpdate'):
            fail('Production is on a safety hold. Ask the Safety Rover to release it.')
        roles = {'label', 'inspection', 'rework', 'sealing', 'final', 'stocker', 'firstMover',
                 'finalMover', 'mixing', 'safety', 'coordinator'} | {'fill:'+str(st['id']) for st in s['stations']}

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
            person = next((p for p in s['people'] if p['id'] == person_id), None)
            if not name or len(name)>80 or person_id and not person or mode not in roles|{'','dashboard','deliveryDisplay'}:
                fail('Choose a valid device name, person and station/view.')
            before = copy.deepcopy(device)
            device.update(name=name, personId=person_id, mode=mode, version=device['version']+1,
                          updatedAt=when, updatedBy=operator)
            extra = {}
            if person and mode in roles and mode not in person['assignments']:
                extra['personBefore'] = copy.deepcopy(person)
                person['assignments'].append(mode)
                person.update(version=person.get('version',0)+1, updatedAt=when, updatedBy=operator)
                extra['personAfter'] = copy.deepcopy(person)
            return result('Device assignment updated', detail=name, deviceBefore=before,
                          deviceAfter=copy.deepcopy(device), **extra)

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
            status = a.get('status')
            if status not in ('available', 'working', 'break', 'off'):
                fail('Select a valid operator status.')
            before = copy.deepcopy(person)
            person.update(assignments=assignments(), status=status, updatedAt=when, updatedBy=operator,
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
                        message['targetType'] == 'delivery' and any(r in person['assignments'] for r in ('stocker','firstMover','finalMover')))
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
            return result('Labeled bags picked up', qty=n, detail='To filling '+str(station['id']), movementId=m['id'])
        if action == 'receiveLabels':
            m = next((x for x in s['moves'] if x['id'] == a.get('movement')), None)
            if not m or m['received']:
                fail('Pickup already received or not found.')
            st = next(x for x in s['stations'] if x['id'] == m['station'])
            st['empty'] += m['qty']
            m.update(received=True, receivedAt=when, receivedBy=operator)
            return result('Empty bags received', qty=m['qty'], detail='Filling '+str(st['id']), movementId=m['id'])
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
            move(bs, routes[source])
            return result('Transport pickup', bs, source+' → '+routes[source], source=source, destination=routes[source])
        if action == 'receive':
            routes = dict(inspectionTransit='inspection', reworkTransit='rework', returnTransit='inspection',
                          sealTransit='sealing', finalTransit='final')
            source = a.get('source')
            if source not in routes:
                fail('Invalid receipt route.')
            bs = take(source)
            move(bs, routes[source])
            return result('Transport received', bs, source+' → '+routes[source], source=source, destination=routes[source])
        if action in ('weigh', 'clear'):
            b = bag(['inspection' if action == 'weigh' else 'rework'])
            if queue(b['status'])[0]['id'] != b['id']:
                fail('Process the oldest bag first.')
            if action == 'weigh' and not isinstance(a.get('labelOK'), bool):
                fail('Record the label check.')
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
                return result('Rework cleared for reinspection', [b], f'{oz:.4f} oz net', massOz=oz)
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
            return result('Run archived', detail='Next run: '+name)
        if action == 'reconcile':
            safety()
            reason = note()
            bs = [b for b in s['bags'] if b['status'] not in ('done', 'discarded')]
            for b in bs:
                b['discardReason'] = 'End-of-run reconciliation: '+reason
            move(bs, 'discarded')
            inventory = dict(labels=s['labels'], stations=copy.deepcopy(s['stations']), inTransit=[m for m in s['moves'] if not m['received']])
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
                    rows.append([s['runId'],b['id'],b['station'],b['mixBatch'],m['id'],m['kind'],time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(m['at']/1000)),m['operator'],m['entered'],m['unit'],m['oz'],m['oz']*OZ,s['limits']['min']-1e-9<=m['oz']<=s['limits']['max']+1e-9])
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


class Handler(BaseHTTPRequestHandler):
    server_version = 'TrailMix/1.0'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        # Do not log access codes, PINs, cookies, or request bodies.
        print('%s %s' % (self.log_date_time_string(), fmt % args))

    def respond(self, code, body, content='application/json', cookie=None, filename=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        if isinstance(body, str):
            body = body.encode()
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
        self.wfile.write(body)

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
                initial_state = self.server.store.state()
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.send_header('Cache-Control','no-cache, no-transform')
                self.send_header('X-Accel-Buffering','no')
                self.send_header('Connection','keep-alive')
                self.end_headers()
                self.wfile.write(b'retry: 10000\n\n')
                def emit(kind, data):
                    self.wfile.write(('event: '+kind+'\ndata: '+json.dumps(data)+'\n\n').encode())
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
                            initial_state = self.server.store.state()
                            emit('state',initial_state)
                        else:
                            devices = copy.deepcopy(initial_state['devices'])
                            self.server.store.attach_presence({'devices':devices})
                            emit('ping',dict(at=NOW(), devices=devices))
                except (BrokenPipeError,ConnectionResetError,TimeoutError,RemoteDatabaseError):
                    pass
                return
            if path == '/api/state':
                self.server.store.touch_device(params.get('deviceId',[''])[0])
                self.respond(200, self.server.store.state())
            elif path == '/api/runs':
                self.respond(200, self.server.store.runs())
            elif path == '/api/export':
                kind = params.get('kind', ['events'])[0]
                mime, data = self.server.store.export(kind, run)
                self.respond(200, data, mime, filename='trailmix-'+kind+('.json' if kind=='json' else '.csv'))
            else:
                self.respond(404, {'error':'Not found.'})
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
                self.respond(200, self.server.store.device_hello(req))
                return
            if self.path != '/api/action':
                self.respond(404, {'error':'Not found.'})
                return
            self.respond(200, self.server.store.command(req))
        except RemoteDatabaseError:
            self.respond(503, {'error':'Database unavailable or save not confirmed. Retry the same save.', 'retryable':True})
        except (Problem, ValueError, TypeError) as e:
            self.respond(409, {'error':str(e)})
        except Exception:
            import traceback
            traceback.print_exc()
            self.respond(500, {'error':'Server could not save the action. No partial update was committed.'})


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
