"""Lossless remote snapshots and compact live views with complete audit exports."""
import copy
import json
import unittest
import server


class SnapshotTests(unittest.TestCase):
    def test_large_remote_snapshot_round_trips_without_losing_audit_history(self):
        store = server.Store.__new__(server.Store)
        store.turso_url = 'libsql://example.test'
        state = server.fresh()
        bag = dict(id=1,station=1,status='done',measurements=[dict(oz=2.1,at=1234,operator='Tester')])
        state['bags'] = [bag]
        state['events'] = [dict(id=str(i),requestId='request-'+str(i),action='finish',
            bags=[1],before=[dict(bag,status='final')],after=[copy.deepcopy(bag)],
            changes={'bags':[1]},at=i,operator='Tester') for i in range(3300)]
        original = copy.deepcopy(state)
        raw = json.dumps(state)
        packed = store.encode_state(state)
        self.assertLess(len(packed),len(raw)//4)
        self.assertEqual(store.decode_state((packed,)), original)
        self.assertEqual(store.decode_state((raw,)), original)  # Prior deployments.
        self.assertEqual(state, original)
        store.turso_url = None
        self.assertEqual(json.loads(store.encode_state(state)), original)

    def test_live_projection_keeps_event_metadata_without_mutating_audit(self):
        event = dict(id='event',action='fill',bags=[1],data={'station':2},
                     changes={'movementId':3},before=[],after=[{'id':1}])
        state = dict(runId='run',events=[event],bags=[{'id':1}])
        original = copy.deepcopy(state)
        response = server.live_response(dict(state=state,results=[dict(requestId='request',event=event)]))
        self.assertEqual(response['state']['events'][0]['data'],{'station':2})
        self.assertEqual(response['results'][0]['event']['bags'],[1])
        self.assertEqual(response['results'][0]['event']['changes'],{'movementId':3})
        self.assertNotIn('before',response['state']['events'][0])
        self.assertNotIn('after',response['results'][0]['event'])
        self.assertEqual(state,original)
