import copy
import unittest
from server import Problem
import test_server as production


class QueuedTests(unittest.TestCase):
    setUp = production.ProductionTests.setUp
    tearDown = production.ProductionTests.tearDown
    req = production.ProductionTests.req
    act = production.ProductionTests.act
    stocked = production.ProductionTests.stocked
    def queued(self, action, **args):
        request = self.req(action, **args)
        request['queued'] = True
        request['recordedAt'] = 123456789
        request['recordedStation'] = 'inspection'
        return request

    def test_unrelated_station_update_does_not_block_queued_weight(self):
        self.stocked(2)
        self.act('fill',station=1)
        self.act('move',source='filled',qty=1)
        bag=self.act('receive',source='inspectionTransit',qty=1)['bags'][0]['id']
        request=self.queued('weigh',bag=bag,mass=2.1,unit='oz',labelOK=True)
        self.act('label',qty=10)
        response=self.store.command(request)
        self.assertEqual(response['state']['bags'][0]['status'],'ready')
        self.assertEqual(response['event']['recordedAt'],123456789)
        self.assertEqual(response['event']['requestId'],request['requestId'])
        self.assertEqual(len(response['state']['bags'][0]['measurements']),1)
        self.assertTrue(self.store.command(request)['duplicate'])

    def test_queued_delivery_never_substitutes_different_bags(self):
        self.stocked(3)
        for _ in range(3):self.act('fill',station=1)
        bags=self.store.state()['bags']
        request=self.queued('move',source='filled',qty=1,station=1,expectedBagIds=[bags[0]['id']])
        self.act('move',source='filled',qty=1,station=1)
        with self.assertRaisesRegex(Problem,'selected bags changed'):
            self.store.command(request)
        self.assertEqual([b['status'] for b in self.store.state()['bags']],['inspectionTransit','filled','filled'])

    def test_queued_delivery_accepts_other_station_updates(self):
        self.stocked(2)
        self.act('fill',station=1)
        bag=self.store.state()['bags'][0]['id']
        request=self.queued('move',source='filled',qty=1,station=1,expectedBagIds=[bag])
        self.act('label',qty=4)
        self.assertEqual(self.store.command(request)['state']['bags'][0]['status'],'inspectionTransit')

    def test_queued_fill_keeps_batch_and_inventory_validation(self):
        self.stocked(2)
        request=self.queued('fill',station=1,mixBatch='MIX-001')
        self.act('assignBatch',station=1,batch='MIX-NEW')
        with self.assertRaisesRegex(Problem,'mixing batch changed'):self.store.command(request)
        self.assertEqual(self.store.state()['stations'][0]['empty'],2)

    def test_queue_cannot_bypass_safety_or_run_change(self):
        request=self.queued('label',qty=1)
        self.act('safetyReport',note='Stop')
        with self.assertRaisesRegex(Problem,'safety hold'):self.store.command(request)
        self.act('safetyRelease',note='Reviewed',safetyPin='rover-pin')
        self.act('newRun',name='Next',safetyPin='rover-pin')
        with self.assertRaisesRegex(Problem,'active run changed'):self.store.command(request)

    def test_queued_delivery_requires_explicit_targets(self):
        request=self.queued('move',source='filled',qty=1)
        with self.assertRaisesRegex(Problem,'exact bags'):self.store.command(request)
