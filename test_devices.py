import json
import tempfile
import unittest
import uuid
from pathlib import Path
from server import Store, Problem


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/'devices.sqlite3','rover-pin')
        self.did = 'dev-test-browser-01'

    def tearDown(self):
        self.tmp.cleanup()

    def act(self, action, device=None, **args):
        s = self.store.state()
        if action == 'deviceUpdate': args['device'] = device
        return self.store.command(dict(action=action, operator='Coordinator', args=args,
            deviceId=device, requestId=str(uuid.uuid4()), runId=s['runId'], revision=s['revision']))

    def hello(self):
        return self.store.device_hello(dict(deviceId=self.did,mode='fill:1',operator='Charlie'))

    def test_register_once_and_assign_device_adds_person_role(self):
        self.hello();s=self.hello()
        self.assertEqual(len(s['devices']),1)
        self.assertEqual(sum(e['action']=='deviceRegister' for e in s['events']),1)
        self.assertTrue(s['devices'][0]['online'])
        self.assertEqual(s['devices'][0]['observedMode'],'fill:1')
        person=self.act('personAdd',name='Charlie',assignments=[])['state']['people'][0]['id']
        result=self.act('deviceUpdate',device=self.did,expectedVersion=0,name='Filler laptop 1',person=person,mode='fill:1')
        self.assertEqual(result['state']['devices'][0]['personId'],person)
        self.assertIn('fill:1',result['state']['people'][0]['assignments'])
        self.assertEqual(result['event']['changes']['deviceBefore']['version'],0)
        self.assertEqual(result['event']['changes']['deviceAfter']['version'],1)

    def test_device_identity_kept_in_activity_and_old_names_preserved(self):
        self.hello()
        self.act('deviceUpdate',device=self.did,expectedVersion=0,name='Inspection tablet',person='',mode='inspection')
        event=self.act('label',device=self.did,qty=1)['event']
        self.assertEqual(event['deviceId'],self.did)
        self.assertEqual(event['deviceName'],'Inspection tablet')
        self.act('deviceUpdate',device=self.did,expectedVersion=1,name='Renamed tablet',person='',mode='inspection')
        stored=next(e for e in self.store.state()['events'] if e['id']==event['id'])
        self.assertEqual(stored['deviceName'],'Inspection tablet')

    def test_stale_device_edit_and_bad_registration_rejected(self):
        self.hello()
        self.act('deviceUpdate',device=self.did,expectedVersion=0,name='Screen',person='',mode='deliveryDisplay')
        with self.assertRaises(Problem):
            self.act('deviceUpdate',device=self.did,expectedVersion=0,name='Old edit',person='',mode='dashboard')
        with self.assertRaises(Problem):
            self.store.device_hello(dict(deviceId='<invalid>'))

    def test_older_database_adds_device_fields_without_deleting_records(self):
        self.act('label',qty=2)
        s=self.store.state();s.pop('devices')
        with self.store.connect() as db:
            db.execute('UPDATE runs SET state=? WHERE id=?',(json.dumps(s),s['runId']))
        self.assertEqual(self.store.state()['labels'],2)
        self.assertEqual(self.store.state()['devices'],[])
        self.hello()
        self.assertEqual(self.store.state()['labels'],2)

    def test_devices_carry_forward_and_server_restart_resets_presence(self):
        self.hello()
        self.act('newRun',name='Run 02',safetyPin='rover-pin')
        self.assertEqual(self.store.state()['devices'][0]['id'],self.did)
        reopened=Store(self.store.path,'rover-pin')
        self.assertFalse(reopened.state()['devices'][0]['online'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
