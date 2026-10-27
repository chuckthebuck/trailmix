import concurrent.futures
import csv
import io
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from server import Store, Problem, OZ


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/'test.sqlite3', 'rover-pin')

    def tearDown(self):
        self.tmp.cleanup()

    def req(self, action, **args):
        s = self.store.state()
        return dict(action=action,args=args,operator='Test operator',requestId=str(uuid.uuid4()),runId=s['runId'],revision=s['revision'])

    def act(self, action, **args):
        return self.store.command(self.req(action, **args))['state']

    def stocked(self, n=3):
        self.act('label', qty=n)
        s=self.act('labelPickup', qty=n, station=1)
        self.act('receiveLabels', movement=s['moves'][0]['id'])
        self.act('assignBatch', station=1, batch='MIX-001')

    def inspected(self, mass=2.1, label=True):
        self.stocked()
        s=self.act('fill', station=1)
        b=s['bags'][-1]['id']
        self.act('move', source='filled', station=1, qty=1)
        self.act('receive', source='inspectionTransit', qty=1)
        self.act('weigh', bag=b, mass=mass, unit='oz', labelOK=label)
        return b

    def test_empty_inventory_cannot_be_filled_or_duplicated(self):
        with self.assertRaises(Problem): self.act('fill', station=1)
        self.stocked(1)
        self.act('fill', station=1)
        with self.assertRaises(Problem): self.act('fill', station=1)
        s=self.store.state()
        self.assertEqual(len(s['bags']),1)
        self.assertEqual(s['stations'][0]['empty'],0)

    def test_pickup_and_receipt_do_not_double_count(self):
        self.act('label',qty=4)
        s=self.act('labelPickup',qty=4,station=1)
        self.assertEqual(s['stations'][0]['empty'],0)
        mid=s['moves'][0]['id']
        self.act('receiveLabels',movement=mid)
        with self.assertRaises(Problem): self.act('receiveLabels',movement=mid)
        self.assertEqual(self.store.state()['stations'][0]['empty'],4)

    def test_both_over_and_underweight_rework_then_direct_sealing(self):
        for mass in (1.9,2.3):
            with self.subTest(mass=mass):
                # Fresh database for each complete flow.
                self.store=Store(Path(self.tmp.name)/(str(mass)+'.sqlite3'),'rover-pin')
                bid=self.inspected(mass)
                self.assertEqual(self.store.state()['bags'][0]['status'],'hold')
                self.act('move',source='hold',qty=1)
                self.assertEqual(self.store.state()['bags'][0]['status'],'reworkTransit')
                self.act('receive',source='reworkTransit',qty=1)
                with self.assertRaisesRegex(Problem,'before delivery'):self.act('clear',bag=bid,mass=2.4,unit='oz')
                self.assertEqual(self.store.state()['bags'][0]['status'],'rework')
                s=self.act('clear',bag=bid,mass=2.1,unit='oz')
                self.assertEqual(s['bags'][0]['status'],'returnReady')
                with self.assertRaises(Problem): self.act('seal',bag=bid)
                self.act('move',source='returnReady',qty=1)
                s=self.act('receive',source='returnTransit',qty=1)
                self.assertEqual(s['bags'][0]['status'],'sealing')
                with self.assertRaises(Problem):self.act('weigh',bag=bid,mass=2.1,unit='oz',labelOK=True)
                self.assertEqual(len(s['bags'][0]['measurements']),2)
                self.assertTrue(s['bags'][0]['firstFail'])

    def test_label_failure_is_discarded_even_if_also_underweight(self):
        self.inspected(1.9,False)
        self.assertEqual(self.store.state()['bags'][0]['status'],'discarded')
        with self.assertRaises(Problem): self.act('move',source='hold',qty=1)

    def test_gram_conversion_and_inclusive_boundaries(self):
        for oz in (2.0,2.25):
            self.store=Store(Path(self.tmp.name)/(str(oz)+'.sqlite3'),'rover-pin')
            self.stocked(1)
            bid=self.act('fill',station=1)['bags'][0]['id']
            self.act('move',source='filled',qty=1)
            self.act('receive',source='inspectionTransit',qty=1)
            s=self.act('weigh',bag=bid,mass=oz*OZ,unit='g',labelOK=True)
            self.assertEqual(s['bags'][0]['status'],'ready')

    def test_exactly_once_retries_and_conflicting_request_id(self):
        req=self.req('label',qty=5)
        self.store.command(req)
        self.assertTrue(self.store.command(req)['duplicate'])
        self.assertEqual(self.store.state()['labels'],5)
        req['args']['qty']=6
        with self.assertRaises(Problem): self.store.command(req)

    def test_concurrent_writes_conflict_without_lost_inventory(self):
        reqs=[self.req('label',qty=1) for _ in range(8)]
        def submit(req):
            try: self.store.command(req); return True
            except Problem: return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(submit,reqs))
        self.assertEqual(sum(results),1)
        self.assertEqual(self.store.state()['labels'],1)
        for _ in range(7): self.act('label',qty=1)
        self.assertEqual(self.store.state()['labels'],8)

    def test_seal_failure_requires_safety_and_all_final_checks_required(self):
        bid=self.inspected()
        self.act('move',source='ready',qty=1)
        self.act('receive',source='sealTransit',qty=1)
        self.act('failSeal',bag=bid,note='incomplete seal')
        with self.assertRaises(Problem): self.act('seal',bag=bid)
        with self.assertRaises(Problem): self.act('disposition',bag=bid,destination='reseal',bagLabelOK=True,note='approved',safetyPin='wrong')
        self.act('disposition',bag=bid,destination='reseal',bagLabelOK=True,note='approved',safetyPin='rover-pin')
        self.act('seal',bag=bid)
        self.act('move',source='sealed',qty=1)
        self.act('receive',source='finalTransit',qty=1)
        with self.assertRaises(Problem): self.act('finish',bag=bid,checks={'seal':True})
        s=self.act('finish',bag=bid,checks={k:True for k in ('seal','label','leak','trappedProduct','contamination')})
        self.assertEqual(s['bags'][0]['status'],'done')
        self.assertTrue(s['bags'][0]['completedAt'])

    def test_any_operator_can_hold_but_only_safety_can_release(self):
        self.act('safetyReport',note='glove contamination')
        with self.assertRaises(Problem): self.act('label',qty=1)
        with self.assertRaises(Problem): self.act('safetyRelease',note='resolved',safetyPin='wrong')
        self.act('safetyRelease',note='resolved',safetyPin='rover-pin')
        self.act('label',qty=1)

    def test_archives_exports_append_only_and_no_pin_in_history(self):
        self.inspected(1.9)
        before=self.store.state()
        self.act('reconcile',note='remaining product discarded, empty bags returned',safetyPin='rover-pin')
        self.act('newRun',name='Run 02',safetyPin='rover-pin')
        self.assertNotEqual(self.store.state()['runId'],before['runId'])
        archive=self.store.state(before['runId'])
        self.assertEqual(archive['events'][-1]['type'],'Run archived')
        self.assertNotIn('rover-pin',json.dumps(archive))
        self.assertEqual(len(archive['bags'][0]['measurements']),1)
        for kind in ('events','weights','bags','json'):
            mime, data=self.store.export(kind,before['runId'])
            self.assertGreater(len(data),50)
        with self.store.connect() as db:
            with self.assertRaises(sqlite3.IntegrityError): db.execute('DELETE FROM events')
        reopened=Store(self.store.path,'rover-pin')
        self.assertEqual(reopened.state(before['runId'])['runId'],before['runId'])

    def test_fifo_rework_and_failed_commands_roll_back(self):
        self.stocked(2)
        for _ in range(2): self.act('fill',station=1)
        self.act('move',source='filled',qty=2)
        s=self.act('receive',source='inspectionTransit',qty=2)
        ids=[b['id'] for b in s['bags']]
        for bid in ids: self.act('weigh',bag=bid,mass=1.8,unit='oz',labelOK=True)
        self.act('move',source='hold',qty=2)
        self.act('receive',source='reworkTransit',qty=2)
        old=self.store.state()
        with self.assertRaises(Problem): self.act('clear',bag=ids[1],mass=2.1,unit='oz')
        self.assertEqual(old,self.store.state())

    def test_csv_formula_safety_and_rebag_lineage(self):
        bid=self.inspected()
        self.act('move',source='ready',qty=1)
        self.act('receive',source='sealTransit',qty=1)
        self.act('failSeal',bag=bid,note='=unsafe spreadsheet formula')
        s=self.act('disposition',bag=bid,destination='rebag',note='damaged bag',safetyPin='rover-pin')
        self.assertEqual(s['bags'][0]['replacedBy'],s['bags'][1]['id'])
        self.assertEqual(s['bags'][1]['replaces'],bid)
        self.assertEqual(s['bags'][1]['status'],'filled')
        _, data=self.store.export('events')
        self.assertIn("'=unsafe spreadsheet formula", data.decode('utf-8-sig'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
