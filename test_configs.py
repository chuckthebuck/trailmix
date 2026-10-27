import unittest
import test_server as production
from server import Store, Problem


class ConfigurationTests(unittest.TestCase):
    tearDown=production.ProductionTests.tearDown
    req=production.ProductionTests.req
    act=production.ProductionTests.act

    def setUp(self):
        production.ProductionTests.setUp(self)
        self.act('personAdd',name='Coordinator',assignments=['coordinator'])

    def coord(self,action,**args):
        req=self.req(action,**args);req['operator']='Coordinator'
        return self.store.command(req)['state']

    def test_named_configuration_reuses_setup_but_no_production_data(self):
        alice=self.act('personAdd',name='Alice',assignments=['fill:2','delivery:filled:2'])['people'][-1]['id']
        self.act('station')
        self.act('deviceRegister',device='config-tablet-1')
        self.act('deviceUpdate',device='config-tablet-1',expectedVersion=0,name='Filling tablet',person=alice,mode='fill:2')
        self.act('assignBatch',station=2,batch='OLD-MIX')
        saved=self.coord('configSave',name='Production setup')['configs'][0]
        self.act('personUpdate',person=alice,assignments=['fill:3'],status='working')
        device=self.store.state()['devices'][0]
        self.act('deviceUpdate',device=device['id'],expectedVersion=device['version'],name='Changed tablet',person=alice,mode='fill:3')
        old_device_version=self.store.state()['devices'][0]['version']
        self.act('station')
        self.act('label',qty=2)
        movement=self.act('labelPickup',station=2,qty=2)['moves'][-1]['id']
        self.act('receiveLabels',movement=movement)
        self.act('fill',station=2)
        old_run=self.store.state()['runId']
        self.act('reconcile',note='Practice run complete',safetyPin='rover-pin')
        state=self.act('newRun',name='Next run',safetyPin='rover-pin',configId=saved['id'])
        self.assertEqual(len(state['stations']),4)
        self.assertTrue(all(st['empty']==0 and st['batch']=='' for st in state['stations']))
        self.assertEqual(next(p for p in state['people'] if p['id']==alice)['assignments'],['fill:2','delivery:filled:2'])
        self.assertEqual(state['devices'][0]['mode'],'fill:2')
        self.assertGreater(state['devices'][0]['version'],old_device_version)
        for key in ('bags','moves','deliveries','events'):self.assertEqual(state[key],[])
        self.assertEqual(state['labels'],0)
        self.assertEqual(state['configs'][0]['id'],saved['id'])
        self.assertEqual(Store(self.store.path).state()['configs'][0]['id'],saved['id'])
        self.assertTrue(self.store.state(old_run)['bags'])
        definition=saved['definition']
        self.assertFalse(any(key in definition for key in ('bags','events','limits','safetyPin','auth_cookie')))

    def test_configuration_requires_coordinator_and_overwrite_version(self):
        with self.assertRaisesRegex(Problem,'coordinator'):self.act('configSave',name='Not allowed')
        first=self.coord('configSave',name='Saved')['configs'][0]
        with self.assertRaisesRegex(Problem,'changed'):self.coord('configSave',name='Saved')
        state=self.coord('configSave',name='Saved',expectedVersion=first['version'])
        self.assertEqual(len(state['configs']),1)
        self.assertEqual(state['configs'][0]['version'],1)
        with self.assertRaises(Problem):self.act('newRun',name='Bad',safetyPin='rover-pin',configId='missing')

    def test_station_assignment_can_replace_and_optionally_configure_device(self):
        alice=self.act('personAdd',name='Alice',assignments=['fill:1','mixing'])['people'][-1]['id']
        bob=self.act('personAdd',name='Bob',assignments=['inspection'])['people'][-1]['id']
        self.act('deviceRegister',device='station-tablet-1')
        with self.assertRaisesRegex(Problem,'coordinator'):
            self.act('assignAtStation',person=bob,expectedVersion=0,stationRole='fill:1')
        state=self.coord('assignAtStation',person=bob,expectedVersion=0,stationRole='fill:1',replace=True,device='station-tablet-1')
        self.assertEqual(next(p for p in state['people'] if p['id']==alice)['assignments'],['mixing'])
        person=next(p for p in state['people'] if p['id']==bob)
        self.assertEqual(set(person['assignments']),{'fill:1','inspection'})
        self.assertEqual(person['status'],'working')
        self.assertEqual(state['devices'][0]['personId'],bob)
        self.assertEqual(state['devices'][0]['mode'],'fill:1')
        before=self.store.state()
        with self.assertRaisesRegex(Problem,'changed'):
            self.coord('assignAtStation',person=bob,expectedVersion=0,stationRole='fill:2')
        self.assertEqual(self.store.state(),before)
