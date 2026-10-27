import copy
import unittest
import uuid
import test_server as production
from server import Problem

class BatchTests(unittest.TestCase):
    setUp=production.ProductionTests.setUp
    tearDown=production.ProductionTests.tearDown
    req=production.ProductionTests.req
    act=production.ProductionTests.act
    stocked=production.ProductionTests.stocked

    def queued(self,action,**args):
        req=self.req(action,**args)
        req.update(queued=True,recordedAt=12345,recordedStation='fill:1')
        return req

    def test_fifty_fills_use_one_batch_and_keep_fifty_individual_records(self):
        self.stocked(50)
        batch=[self.queued('fill',station=1,mixBatch='MIX-001') for _ in range(50)]
        result=self.store.commands(batch)
        self.assertEqual(len(result['results']),50)
        self.assertEqual(len(result['state']['bags']),50)
        self.assertEqual(result['state']['stations'][0]['empty'],0)
        self.assertEqual(len({e['requestId'] for e in result['state']['events'] if e['action']=='fill'}),50)
        retry=self.store.commands(batch)
        self.assertTrue(all(r['duplicate'] for r in retry['results']))
        self.assertEqual(len(retry['state']['bags']),50)

    def test_dependent_fills_pickup_receipt_and_weights_resolve_pending_ids(self):
        self.stocked(3)
        batch=[self.queued('fill',station=1,mixBatch='MIX-001') for _ in range(3)]
        ids=['pending-'+req['requestId'] for req in batch]
        batch.extend([self.queued('move',source='filled',station=1,qty=3,expectedBagIds=ids),
                      self.queued('receive',source='inspectionTransit',station=1,qty=3,expectedBagIds=ids)])
        batch.extend(self.queued('weigh',bag=id,mass=mass,unit='oz',labelOK=True) for id,mass in zip(ids,[2.1,1.7,2.4]))
        result=self.store.commands(batch)
        self.assertEqual([b['status'] for b in result['state']['bags']],['ready','hold','hold'])
        self.assertTrue(all(isinstance(b['id'],int) for b in result['state']['bags']))
        self.assertEqual(len(result['results'][0]['event']['after'][0]['measurements']),0)

    def test_bad_action_rolls_back_every_new_entry(self):
        self.stocked(2)
        before=self.store.state()
        batch=[self.queued('fill',station=1,mixBatch='MIX-001'),self.queued('fill',station=1,mixBatch='wrong')]
        with self.assertRaises(Problem):self.store.commands(batch)
        self.assertEqual(before,self.store.state())
        batch[1]['args']['mixBatch']='MIX-001'
        self.assertEqual(len(self.store.commands(batch)['state']['bags']),2)

    def test_partial_previous_commit_and_duplicate_in_changed_batch(self):
        self.stocked(2)
        first=self.queued('fill',station=1,mixBatch='MIX-001')
        self.store.command(first)
        second=self.queued('fill',station=1,mixBatch='MIX-001')
        result=self.store.commands([first,second])
        self.assertTrue(result['results'][0]['duplicate'])
        self.assertFalse(result['results'][1]['duplicate'])
        self.assertEqual(len(result['state']['bags']),2)
        altered=copy.deepcopy(first);altered['args']['station']=2
        with self.assertRaises(Problem):self.store.commands([altered])

    def test_limits_and_duplicate_ids_and_management_actions_are_rejected(self):
        req=self.queued('label',qty=1)
        for batch in ([],[req]*51,[req,req],[self.queued('personAdd',name='No')]):
            with self.assertRaises(Problem):self.store.commands(batch)
