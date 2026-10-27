import copy
import json
import sqlite3
import unittest
import uuid
import test_server as production
from server import Store, Problem, OZ


class RouteTests(unittest.TestCase):
    setUp=production.ProductionTests.setUp
    tearDown=production.ProductionTests.tearDown
    req=production.ProductionTests.req
    act=production.ProductionTests.act
    stocked=production.ProductionTests.stocked

    def mover(self,name,roles):
        return self.act('personAdd',name=name,assignments=roles)['people'][-1]['id']

    def as_person(self,name,action,**args):
        req=self.req(action,**args);req['operator']=name
        return self.store.command(req)

    def pickup(self,name,source,station,qty):
        state=self.store.state()
        bags=sorted((b for b in state['bags'] if b['status']==source and b['station']==station),key=lambda b:(b['updated'],b['id']))[:qty]
        return self.as_person(name,'routePickup',source=source,station=station,qty=qty,expectedBagIds=[b['id'] for b in bags])

    def test_assignment_per_leg_per_station_and_two_separate_handoffs(self):
        self.stocked(3)
        for _ in range(3):self.act('fill',station=1)
        self.mover('Mover A',['delivery:filled:1'])
        self.mover('Mover B',['delivery:filled:2'])
        with self.assertRaisesRegex(Problem,'not assigned'):self.pickup('Mover B','filled',1,1)
        result=self.pickup('Mover A','filled',1,2)
        package=result['state']['deliveries'][-1]
        self.assertEqual(package['pickedBy'],'Mover A')
        self.assertEqual(package['remainingBagIds'],result['event']['bags'])
        self.assertEqual([b['status'] for b in result['state']['bags']],['inspectionTransit','inspectionTransit','filled'])
        with self.assertRaisesRegex(Problem,'different operator'):
            self.as_person('Mover B','routeDeliver',delivery=package['id'])
        state=self.as_person('Mover A','routeDeliver',delivery=package['id'])['state']
        self.assertEqual([b['status'] for b in state['bags']],['inspection','inspection','filled'])
        self.assertEqual(state['deliveries'][-1]['status'],'delivered')
        self.assertEqual(state['deliveries'][-1]['deliveredBy'],'Mover A')
        with self.assertRaises(Problem):self.as_person('Mover A','routeDeliver',delivery=package['id'])

    def test_package_delivery_does_not_receive_another_operators_older_load(self):
        self.stocked(2)
        for _ in range(2):self.act('fill',station=1)
        for name in ('Older','Newer'):self.mover(name,['delivery:filled:1'])
        first=self.pickup('Older','filled',1,1)['state']['deliveries'][-1]
        second=self.pickup('Newer','filled',1,1)['state']['deliveries'][-1]
        state=self.as_person('Newer','routeDeliver',delivery=second['id'])['state']
        self.assertEqual(state['bags'][0]['status'],'inspectionTransit')
        self.assertEqual(state['bags'][1]['status'],'inspection')
        self.assertEqual(next(d for d in state['deliveries'] if d['id']==first['id'])['status'],'transit')

    def test_batch_label_pickup_and_delivery_remap_package_and_movement(self):
        self.act('label',qty=5)
        self.mover('Stocker',['delivery:labels:2'])
        first=self.req('routePickup',source='labels',station=2,qty=5)
        second=self.req('routeDeliver',delivery='pending-'+first['requestId'])
        for req in (first,second):req.update(operator='Stocker',queued=True)
        result=self.store.commands([first,second])
        self.assertEqual(result['state']['stations'][1]['empty'],5)
        self.assertEqual(result['state']['deliveries'][-1]['status'],'delivered')
        self.assertTrue(result['state']['moves'][-1]['received'])
        retry=self.store.commands([first,second])
        self.assertEqual(retry['state']['stations'][1]['empty'],5)
        self.assertTrue(all(r['duplicate'] for r in retry['results']))

    def test_normal_station_receipt_updates_and_partially_closes_package(self):
        self.stocked(2)
        for _ in range(2):self.act('fill',station=1)
        self.mover('Mover',['delivery:filled:1'])
        package=self.pickup('Mover','filled',1,2)['state']['deliveries'][-1]
        state=self.act('receive',source='inspectionTransit',station=1,qty=1)
        d=next(d for d in state['deliveries'] if d['id']==package['id'])
        self.assertEqual(len(d['remainingBagIds']),1)
        self.assertEqual(d['status'],'transit')
        state=self.as_person('Mover','routeDeliver',delivery=package['id'])['state']
        self.assertEqual(next(d for d in state['deliveries'] if d['id']==package['id'])['status'],'delivered')
        self.assertEqual(sum(r['qty'] for r in state['deliveries'][-1]['receipts']),2)

    def test_invalid_rework_is_rejected_and_boundaries_work_in_grams(self):
        self.stocked(1)
        bag=self.act('fill',station=1)['bags'][0]['id']
        self.act('move',source='filled',qty=1);self.act('receive',source='inspectionTransit',qty=1)
        self.act('weigh',bag=bag,mass=1.8,unit='oz',labelOK=True)
        self.act('move',source='hold',qty=1);self.act('receive',source='reworkTransit',qty=1)
        before=self.store.state()
        for mass,unit in [(1.99,'oz'),(2.26,'oz'),(2.3*OZ,'g'),(1.99*OZ,'g'),(0,'oz'),(-2,'oz'),(float('inf'),'oz'),(True,'oz')]:
            with self.assertRaisesRegex(Problem,'before delivery'):
                self.act('clear',bag=bag,mass=mass,unit=unit)
            self.assertEqual(before,self.store.state())
        state=self.act('clear',bag=bag,mass=2.25*OZ,unit='g')
        self.assertEqual(state['bags'][0]['status'],'returnReady')
        self.mover('Return mover',['delivery:returnReady:1'])
        result=self.pickup('Return mover','returnReady',1,1)
        self.assertEqual(result['state']['bags'][0]['status'],'returnTransit')

    def test_new_workflow_updates_active_limits_but_preserves_history_and_archive(self):
        # Emulate an old installation: a bag awaiting rework return and one old reading.
        self.stocked(1)
        self.act('fill',station=1)
        state=self.store.state();state['limits']=dict(min=1.5,max=1.75,confirmed=True)
        state['bags'][0].update(status='returnReady',holdAt=1,measurements=[dict(id='old-reading',oz=1.6,kind='rework',at=1,operator='Old',entered=1.6,unit='oz')])
        with self.store.connect() as db:db.execute('UPDATE runs SET state=? WHERE id=?',(json.dumps(state),state['runId']))
        self.store.upgrade_workflow()
        migrated=self.store.state()
        self.assertEqual(migrated['limits']['min'],2)
        self.assertEqual(migrated['bags'][0]['status'],'rework')
        reading=migrated['bags'][0]['measurements'][0]
        self.assertEqual(reading['oz'],1.6);self.assertTrue(reading['passed']);self.assertEqual(reading['minOz'],1.5)
        count=len(migrated['events']);Store(self.store.path).upgrade_workflow()
        self.assertEqual(len(self.store.state()['events']),count)

    def test_delivery_view_mode_does_not_broaden_specific_route_assignments(self):
        person=self.mover('Specific mover',['delivery:filled:2'])
        self.act('deviceRegister',device='route-device-1')
        state=self.act('deviceUpdate',device='route-device-1',expectedVersion=0,name='Mover tablet',person=person,mode='firstMover')
        self.assertEqual(state['people'][-1]['assignments'],['delivery:filled:2'])

    def test_all_six_legs_and_direct_rework_have_separate_package_receipts(self):
        from server import DELIVERY_LEGS
        self.mover('All legs',['delivery:'+source+':1' for source in DELIVERY_LEGS])
        self.act('label',qty=2)
        package=self.pickup('All legs','labels',1,2)['state']['deliveries'][-1]
        self.as_person('All legs','routeDeliver',delivery=package['id'])
        self.act('assignBatch',station=1,batch='ROUTE-MIX')
        bag=self.act('fill',station=1)['bags'][0]['id']
        def handoff(source):
            package=self.pickup('All legs',source,1,1)['state']['deliveries'][-1]
            return self.as_person('All legs','routeDeliver',delivery=package['id'])['state']
        handoff('filled')
        self.act('weigh',bag=bag,mass=2.4,unit='oz',labelOK=True)
        handoff('hold')
        self.act('clear',bag=bag,mass=2.0,unit='oz')
        with self.assertRaises(Problem):self.act('seal',bag=bag)
        handoff('returnReady')
        self.act('seal',bag=bag);state=handoff('sealed')
        self.assertEqual(state['bags'][0]['status'],'final')
        other=self.act('fill',station=1)['bags'][-1]['id']
        handoff('filled');self.act('weigh',bag=other,mass=2.25,unit='oz',labelOK=True)
        state=handoff('ready')
        self.assertEqual({d['source'] for d in state['deliveries']},set(DELIVERY_LEGS))
        self.assertTrue(all(d['status']=='delivered' and len(d['receipts'])==1 for d in state['deliveries']))
        self.assertTrue(self.store.export('deliveries')[1].decode('utf-8-sig').startswith('run_id,delivery_id'))

    def test_operator_can_deliver_own_package_after_reassignment(self):
        self.stocked(1);self.act('fill',station=1)
        person=self.mover('Mover',['delivery:filled:1'])
        package=self.pickup('Mover','filled',1,1)['state']['deliveries'][-1]
        self.act('personUpdate',person=person,status='available',assignments=[])
        self.assertEqual(self.as_person('Mover','routeDeliver',delivery=package['id'])['state']['deliveries'][-1]['status'],'delivered')

    def test_existing_transit_is_imported_as_packages_without_changing_bag_locations(self):
        self.stocked(2);self.act('fill',station=1)
        self.mover('Legacy mover',['delivery:filled:1'])
        self.as_person('Legacy mover','move',source='filled',station=1,qty=1)
        state=self.store.state();state['deliveries']=[]
        with self.store.connect() as db:db.execute('UPDATE runs SET state=? WHERE id=?',(json.dumps(state),state['runId']))
        self.store.upgrade_workflow();state=self.store.state()
        self.assertEqual(state['bags'][0]['status'],'inspectionTransit')
        self.assertEqual(len(state['deliveries']),1)
        package=state['deliveries'][0];self.assertTrue(package['legacy'])
        self.assertEqual(package['pickedBy'],'Legacy mover')
        state=self.as_person('Legacy mover','routeDeliver',delivery=package['id'])['state']
        self.assertEqual(state['bags'][0]['status'],'inspection')

    def test_shared_downstream_package_combines_filling_origins(self):
        self.stocked(1)
        self.act('label',qty=1)
        movement=self.act('labelPickup',station=2,qty=1)['moves'][-1]['id']
        self.act('receiveLabels',movement=movement);self.act('assignBatch',station=2,batch='MIX-2')
        for station in (1,2):self.act('fill',station=station)
        self.act('move',source='filled',qty=2);state=self.act('receive',source='inspectionTransit',qty=2)
        ids=[b['id'] for b in state['bags']]
        for id in ids:self.act('weigh',bag=id,mass=2.4,unit='oz',labelOK=True)
        self.mover('Shared mover',['delivery:hold:*','delivery:returnReady:*'])
        result=self.as_person('Shared mover','routePickup',source='hold',qty=2,expectedBagIds=ids)
        package=result['state']['deliveries'][-1]
        self.assertIsNone(package['station']);self.assertEqual(len(package['bagIds']),2)
        self.as_person('Shared mover','routeDeliver',delivery=package['id'])
        for id in ids:self.act('clear',bag=id,mass=2.1,unit='oz')
        package=self.as_person('Shared mover','routePickup',source='returnReady',qty=2,expectedBagIds=ids)['state']['deliveries'][-1]
        state=self.as_person('Shared mover','routeDeliver',delivery=package['id'])['state']
        self.assertTrue(all(b['status']=='sealing' for b in state['bags']))
        self.assertEqual({b['station'] for b in state['bags']},{1,2})

    def test_route_plan_upgrade_collapses_roles_and_forwards_already_returned_rework(self):
        self.stocked(1);self.act('fill',station=1)
        person=self.mover('Older mover',['delivery:hold:1','delivery:hold:2','delivery:returnReady:2'])
        state=self.store.state();state['bags'][0].update(status='inspection',measurements=[dict(id='old-clear',oz=2.1,kind='rework',at=1,operator='Older mover',entered=2.1,unit='oz')])
        with self.store.connect() as db:db.execute('UPDATE runs SET state=? WHERE id=?',(json.dumps(state),state['runId']))
        self.store.upgrade_route_plan();state=self.store.state()
        self.assertEqual(next(p for p in state['people'] if p['id']==person)['assignments'],['delivery:hold:*','delivery:returnReady:*'])
        self.assertEqual(state['bags'][0]['status'],'ready')
        state=self.act('move',source='ready',qty=1)
        self.assertEqual(state['bags'][0]['status'],'sealTransit')
        before=len(state['events']);Store(self.store.path).upgrade_route_plan()
        self.assertEqual(len(self.store.state()['events']),before)
