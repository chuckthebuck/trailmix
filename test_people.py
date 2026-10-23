import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from server import Store, Problem


class PeopleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/'people.sqlite3', 'rover-pin')

    def tearDown(self):
        self.tmp.cleanup()

    def act(self, action, actor='Coordinator', **args):
        s = self.store.state()
        return self.store.command(dict(action=action, operator=actor, args=args,
            requestId=str(uuid.uuid4()), runId=s['runId'], revision=s['revision']))

    def add(self, name, assignments=()):
        result = self.act('personAdd', name=name, assignments=list(assignments))
        return result['state']['people'][-1]['id']

    def test_multi_role_assignment_history_and_event_attribution(self):
        person = self.add('Charlie', ['stocker', 'firstMover'])
        result = self.act('personUpdate', person=person,
                          assignments=['stocker','firstMover','firstMover'], status='working')
        self.assertEqual(result['event']['changes']['personBefore']['status'],'available')
        self.assertEqual(result['state']['people'][0]['assignments'],['stocker','firstMover'])
        logged = self.act('label', actor='Charlie', qty=2)['event']
        self.assertEqual(logged['personId'],person)
        self.assertEqual(logged['assignments'],['stocker','firstMover'])
        with self.assertRaises(Problem): self.add('charlie')
        with self.assertRaises(Problem): self.add('Other', ['fill:999'])

    def test_direct_message_acknowledgements_and_resolution_are_retained(self):
        alice = self.add('Alice')
        self.add('Bob')
        m = self.act('messagePost', targetType='person',target=alice,text='Bring fresh labeled bags',priority='urgent')['state']['messages'][-1]
        with self.assertRaises(Problem): self.act('messageAcknowledge', actor='Bob',message=m['id'])
        result = self.act('messageAcknowledge', actor='Alice',message=m['id'])
        self.assertEqual(result['state']['messages'][0]['acknowledgements'][0]['personId'],alice)
        with self.assertRaises(Problem): self.act('messageAcknowledge', actor='Alice',message=m['id'])
        result = self.act('messageResolve',message=m['id'])
        self.assertTrue(result['state']['messages'][0]['resolvedAt'])
        self.assertEqual(result['event']['changes']['messageBefore']['resolvedAt'],None)
        self.assertEqual(len(result['state']['messages']),1)

    def test_station_and_delivery_message_audiences(self):
        self.add('Mover',['firstMover'])
        self.add('Inspector',['inspection'])
        for audience,target in [('delivery',''),('station','firstMover')]:
            m=self.act('messagePost',targetType=audience,target=target,text='Filling 2 needs pickup')['state']['messages'][-1]
            self.act('messageAcknowledge',actor='Mover',message=m['id'])
            with self.assertRaises(Problem): self.act('messageAcknowledge',actor='Inspector',message=m['id'])
        m=self.act('messagePost',targetType='all',text='Shift change in five minutes')['state']['messages'][-1]
        self.act('messageAcknowledge',actor='Inspector',message=m['id'])

    def test_personnel_messages_can_be_managed_during_safety_hold(self):
        self.act('safetyReport',note='Practice safety hold')
        person=self.add('Mover',['stocker'])
        self.act('personUpdate',person=person,assignments=['stocker'],status='break')
        m=self.act('messagePost',targetType='all',text='Wait for safety release')['state']['messages'][-1]
        self.act('messageAcknowledge',actor='Mover',message=m['id'])
        with self.assertRaises(Problem): self.act('label',qty=1)

    def test_archive_carries_roster_preserves_message_history(self):
        person=self.add('Mover',['finalMover'])
        self.act('messagePost',targetType='delivery',text='Finish current pickups')
        old=self.store.state()['runId']
        result=self.act('newRun',name='Run 02',safetyPin='rover-pin')
        self.assertEqual(result['state']['people'][0]['id'],person)
        self.assertEqual(result['state']['people'][0]['assignments'],['finalMover'])
        self.assertEqual(result['state']['messages'],[])
        self.assertEqual(len(self.store.state(old)['messages']),1)

    def test_existing_database_uses_new_views_without_losing_records(self):
        self.act('label',qty=5)
        old=copy.deepcopy(self.store.state())
        old.pop('people');old.pop('messages')
        with self.store.connect() as db:
            db.execute('UPDATE runs SET state=? WHERE id=?',(json.dumps(old),old['runId']))
        reopened=Store(self.store.path,'rover-pin')
        self.assertEqual(reopened.state()['labels'],5)
        self.assertEqual(reopened.state()['people'],[])
        self.assertEqual(len(reopened.state()['events']),1)
        self.add('New operator')
        self.assertEqual(self.store.state()['labels'],5)

    def test_message_does_not_invalidate_an_entered_production_action(self):
        initial = self.store.state()
        queued = dict(action='label', operator='Filler', args={'qty':3}, requestId=str(uuid.uuid4()),
                      runId=initial['runId'], revision=initial['revision'])
        self.act('messagePost',targetType='all',text='Use the updated pickup route')
        self.assertEqual(self.store.command(queued)['state']['labels'],3)
        for kind in ('people','messages','events'):
            self.assertGreater(len(self.store.export(kind)[1]),50)

    def test_assignment_changes_reject_an_outdated_person_version(self):
        person=self.add('Mover',['firstMover'])
        self.act('personUpdate',person=person,assignments=['stocker'],status='working',expectedVersion=0)
        with self.assertRaises(Problem):
            self.act('personUpdate',person=person,assignments=['finalMover'],status='break',expectedVersion=0)
        self.assertEqual(self.store.state()['people'][0]['assignments'],['stocker'])

    def test_station_scoped_mover_pickup_keeps_other_station_bags(self):
        self.act('label',qty=5)
        for station,qty in ((1,2),(2,3)):
            s=self.act('labelPickup',station=station,qty=qty)['state']
            self.act('receiveLabels',movement=s['moves'][-1]['id'])
            self.act('assignBatch',station=station,batch='MIX-'+str(station))
            for _ in range(qty): self.act('fill',station=station)
        s=self.act('move',source='filled',station=2,qty=3)['state']
        self.assertEqual(len([b for b in s['bags'] if b['station']==1 and b['status']=='filled']),2)
        self.assertEqual(len([b for b in s['bags'] if b['station']==2 and b['status']=='inspectionTransit']),3)
        self.act('receive',source='inspectionTransit',station=2,qty=3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
