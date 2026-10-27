import tempfile
import unittest
from pathlib import Path
import test_server as production
from server import Store, Problem


class SetupTests(unittest.TestCase):
    setUp = production.ProductionTests.setUp
    tearDown = production.ProductionTests.tearDown
    req = production.ProductionTests.req
    act = production.ProductionTests.act

    def test_create_roster_before_production_with_combined_roles(self):
        state=self.act('personAddMany',names=['Charlie','Liam','Ivo'],assignments=['inspection','firstMover'])
        self.assertEqual(len(state['people']),3)
        self.assertFalse(state['bags'])
        self.assertTrue(all(p['status']=='available' and p['assignments']==['inspection','firstMover'] for p in state['people']))
        self.assertEqual(len(state['events'][-1]['changes']['peopleAfter']),3)
        restarted=Store(self.store.path)
        self.assertEqual(restarted.state()['people'],state['people'])
        self.assertEqual(restarted.safety_pin,'rover-pin')

    def test_roster_and_all_station_roles_carry_into_next_run(self):
        self.act('station')
        state=self.act('personAdd',name='Prepared operator',assignments=['fill:4','stocker'])
        people=state['people']
        state=self.act('newRun',name='Production day',safetyPin='rover-pin')
        self.assertEqual(state['people'],people)
        self.assertEqual([s['id'] for s in state['stations']],[1,2,3,4])
        self.assertTrue(all(s['empty']==0 and s['batch']=='' for s in state['stations']))

    def test_bulk_validation_is_atomic_and_retries_do_not_duplicate_roster(self):
        self.act('personAdd',name='Existing',assignments=[])
        for names in (['One','one'],['Valid','Existing'],[],[''],['Long'*30]):
            with self.assertRaises(Problem):self.act('personAddMany',names=names,assignments=[])
        self.assertEqual(len(self.store.state()['people']),1)
        request=self.req('personAddMany',names=['Prepared A','Prepared B'],assignments=['rework'])
        self.store.command(request)
        self.assertTrue(self.store.command(request)['duplicate'])
        self.assertEqual(len(self.store.state()['people']),3)

    def test_generated_pin_and_cookie_survive_restart_and_explicit_pin_change(self):
        path=Path(self.tmp.name)/'generated.sqlite3'
        first=Store(path)
        self.assertTrue(first.pin_created)
        again=Store(path)
        self.assertFalse(again.pin_created)
        self.assertEqual(first.safety_pin,again.safety_pin)
        self.assertEqual(first.auth_cookie,again.auth_cookie)
        changed=Store(path,'chosen-stable-pin')
        self.assertEqual(Store(path).safety_pin,'chosen-stable-pin')
        self.assertNotIn(changed.safety_pin,changed.export('json')[1].decode())
        self.assertNotIn(changed.auth_cookie,changed.export('json')[1].decode())

    def test_class_roster_seed_is_once_only_and_safety_assignments_are_exact(self):
        self.act('personAdd',name='Charlie Stephen Malone',assignments=[])
        self.store.seed_class_roster()
        state=self.store.state()
        self.assertEqual(len(state['people']),28)
        safety=[p['name'].split()[0].lower() for p in state['people'] if p['assignments']==['safety']]
        self.assertEqual(set(safety),{'charlie','blake','lucas'})
        charlie=next(p for p in state['people'] if p['name'].startswith('Charlie'))
        self.act('personDelete',person=charlie['id'])
        restarted=Store(self.store.path)
        restarted.seed_class_roster()
        self.assertTrue(next(p for p in restarted.state()['people'] if p['id']==charlie['id'])['deletedAt'])
        self.assertEqual(len(restarted.state()['events']),3)

    def test_delete_removes_device_assignment_and_preserves_history_and_can_restore(self):
        state=self.act('personAdd',name='Delete test',assignments=['inspection'])
        person=state['people'][0]['id']
        self.act('deviceRegister',device='delete-device-1')
        self.act('deviceUpdate',device='delete-device-1',expectedVersion=0,name='Tablet',person=person,mode='inspection')
        before=self.store.state()['events'][0]
        state=self.act('personDelete',person=person)
        self.assertTrue(state['people'][0]['deletedAt'])
        self.assertEqual(state['devices'][0]['personId'],'')
        self.assertEqual(state['events'][0],before)
        state=self.act('personRestore',person=person)
        self.assertNotIn('deletedAt',state['people'][0])
        self.assertEqual(state['people'][0]['assignments'],['inspection'])
