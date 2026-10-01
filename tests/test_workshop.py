import copy
from pathlib import Path
import tempfile
import unittest
from fastapi.testclient import TestClient
from functools import partial
from geppetto.app import create_app as _create_app
# These isolated fixtures exercise catalog behavior without browser authentication.
create_app = partial(_create_app, auth_required=False)
from geppetto.workshop import reconcile,topology,LOCAL_WHALE


class WorkshopTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=create_app(Path(self.tmp.name),'demo',start_worker=False)
        self.service=self.app.state.service
        self.client=TestClient(self.app)
        self.headers={'X-Geppetto-Request':'1'}

    def tearDown(self):
        self.client.close();self.tmp.cleanup()

    def pin(self):
        r=self.client.post('/api/whales/whale-local/pinocchios',json={'template_id':'ball-p2-v1'},headers=self.headers)
        self.assertEqual(r.status_code,200,r.text)
        return r.json()

    def body(self,pin):
        return {'name':'model learning','node_id':LOCAL_WHALE,'pinocchio_id':pin['id'],
                'template_id':'ball-p2-v1','dataset_ids':['demo-ball-detection'],'overrides':{}}

    def test_independent_training_creates_distinct_models(self):
        reconcile(self.service.store)
        pin=self.pin()
        ids=[]
        for i in range(2):
            body=self.body(pin)
            body['confirmation_hash']=self.service.preview(body)['confirmation_hash']
            result=self.service.submit(body,'repeat-learning-'+str(i))
            ids.append(result['pinocchio_id'])
            self.assertEqual(result['node_id'],LOCAL_WHALE)
        self.assertEqual(len(topology(self.service)['whale']),1)
        with self.service.store.connect() as db:
            jobs=[r for r in self.service.store.all(db,'run') if r['pinocchio_id']==pin['id']]
        self.assertEqual(len(jobs),1)
        self.assertEqual(ids[0],pin['id'])
        self.assertNotEqual(ids[0],ids[1])

    def test_unconnected_node_cannot_execute_or_borrow_resources(self):
        reconcile(self.service.store)
        node=self.client.post('/api/whales',json={'name':'remote','host':'node-label'},headers=self.headers).json()
        self.assertEqual(node['connection'],'unconnected')
        self.assertEqual(node['template_ids'],[])
        body=self.body(self.pin());body['node_id']=node['id']
        with self.assertRaisesRegex(ValueError,'not connected'):self.service.preview(body)
        result=self.client.post('/api/whales/'+node['id']+'/pinocchios',json={'template_id':'ball-p2-v1'},headers=self.headers)
        self.assertEqual(result.status_code,422)

    def test_identity_is_bound_to_final_confirmation_and_template(self):
        reconcile(self.service.store)
        first=self.pin();second=self.pin()
        body=self.body(first)
        body['confirmation_hash']=self.service.preview(body)['confirmation_hash']
        body['pinocchio_id']=second['id']
        with self.assertRaisesRegex(ValueError,'Review'):self.service.submit(body,'identity-change-1')
        body['template_id']='jersey-vit-v1'
        with self.assertRaisesRegex(ValueError,'must match'):self.service.preview(body)

    def test_migration_is_idempotent_preserves_snapshots_and_schedules(self):
        before=topology(self.service)
        with self.service.store.connect() as db:
            run=self.service.store.all(db,'run')[0]
            snapshot=copy.deepcopy(run['frozen'])
            run.pop('pinocchio_id');run.pop('node_id')
            self.service.store.put(db,'run',run)
        reconcile(self.service.store)
        restored=self.service.get('run',run['id'])
        self.assertEqual(restored['frozen'],snapshot)
        pid=restored['pinocchio_id']
        reconcile(self.service.store)
        self.assertEqual(self.service.get('run',run['id'])['pinocchio_id'],pid)
        pin=self.pin();body=self.body(pin)
        body['schedule']={'kind':'cron','cron':'* * * * *','timezone':'UTC'}
        body['confirmation_hash']=self.service.preview(body)['confirmation_hash']
        schedule=self.service.submit(body,'workshop-schedule')
        with self.service.store.connect() as db:
            self.service.tick_schedules(db,schedule['next_at']+1)
            job=next(r for r in self.service.store.all(db,'run') if r.get('schedule_id')==schedule['id'])
        self.assertEqual(job['pinocchio_id'],pin['id'])
        self.assertEqual(job['node_id'],LOCAL_WHALE)

    def test_evaluation_keeps_parent_model_identity(self):
        reconcile(self.service.store)
        with self.service.store.connect() as db:
            parent=next(r for r in self.service.store.all(db,'run') if r['category']=='ball-detection')
        body={'name':'evaluate','template_id':'ball-p2-v1','dataset_ids':['demo-ball-detection'],
              'parent_id':parent['id'],'relation':'evaluate'}
        frozen=self.service.preview(body)['frozen']
        self.assertEqual(frozen['pinocchio_id'],parent['pinocchio_id'])
        body['pinocchio_id']=self.pin()['id']
        with self.assertRaisesRegex(ValueError,'original Pinocchio'):self.service.preview(body)

    def test_rename_and_project_move_preserve_identity(self):
        reconcile(self.service.store)
        pin=self.pin()
        response=self.client.patch('/api/pinocchios/'+pin['id'],json={'name':'YOLO26 P2 custom'},headers=self.headers)
        self.assertEqual(response.status_code,200)
        response=self.client.patch('/api/whales/'+LOCAL_WHALE,json={'name':'local gpu'},headers=self.headers)
        self.assertEqual(response.status_code,200)
        project=self.client.post('/api/projects',json={'name':'공 비교 실험','category':'ball-detection'},headers=self.headers).json()
        result=self.client.post('/api/pinocchios/'+pin['id']+'/project',json={'project_id':project['id']},headers=self.headers)
        self.assertEqual(result.status_code,200)
        reconcile(self.service.store)
        model=self.service.get('pinocchio',pin['id'])
        self.assertEqual(model['model_id'],pin['id'])
        self.assertEqual(model['name'],'YOLO26 P2 custom')
        self.assertEqual(model['project_id'],project['id'])
        self.assertEqual(self.service.get('whale',LOCAL_WHALE)['name'],'local gpu')
        self.assertEqual(self.client.patch('/api/whales/'+LOCAL_WHALE,json={'name':'  '},headers=self.headers).status_code,422)

    def test_schedule_occurrences_are_distinct_models(self):
        reconcile(self.service.store)
        body=self.body(self.pin());body['schedule']={'kind':'cron','cron':'* * * * *','timezone':'UTC'}
        body['confirmation_hash']=self.service.preview(body)['confirmation_hash']
        schedule=self.service.submit(body,'each-schedule-model')
        with self.service.store.connect() as db:
            self.service.tick_schedules(db,schedule['next_at']+1)
            first=next(r for r in self.service.store.all(db,'run') if r.get('schedule_id')==schedule['id'])
            first['state']='succeeded';self.service.store.put(db,'run',first)
            schedule=self.service.store.get(db,'schedule',schedule['id'])
            self.service.tick_schedules(db,schedule['next_at']+1)
            jobs=[r for r in self.service.store.all(db,'run') if r.get('schedule_id')==schedule['id']]
        self.assertEqual(len({r['pinocchio_id'] for r in jobs}),2)

    def test_migrate_shared_architecture_models_once(self):
        reconcile(self.service.store)
        with self.service.store.connect() as db:
            parent=next(r for r in self.service.store.all(db,'run') if r['category']=='ball-detection')
            other=copy.deepcopy(parent);other['id']='fresh-training';other['created_at']+=1
            other['frozen']['relation']='train';self.service.store.put(db,'run',other)
            evaluation=copy.deepcopy(other);evaluation['id']='evaluation';evaluation['created_at']+=1
            evaluation['frozen'].update(relation='evaluate',parent_id=other['id']);self.service.store.put(db,'run',evaluation)
        reconcile(self.service.store)
        second=self.service.get('run',other['id'])
        self.assertNotEqual(second['pinocchio_id'],parent['pinocchio_id'])
        self.assertEqual(second['pinocchio_id'],self.service.get('run','evaluation')['pinocchio_id'])
        count=len(topology(self.service)['pinocchio']);reconcile(self.service.store)
        self.assertEqual(count,len(topology(self.service)['pinocchio']))

    def test_compare_imported_flat_data_path_and_projects(self):
        reconcile(self.service.store)
        with self.service.store.connect() as db:
            first=next(r for r in self.service.store.all(db,'run') if r['category']=='ball-detection')
            second=copy.deepcopy(first);second['id']='imported-second';second['created_at']+=1
            first['frozen']['config']={'data':'/datasets/data.yaml','imgsz':'1920'}
            second['frozen']['config']={'evaluation.imgsz':'1920'}
            self.service.store.put(db,'run',first);self.service.store.put(db,'run',second)
        result=self.service.compare([first['id'],second['id']])
        self.assertFalse(any('resolution' in w for w in result['warnings']))
        second=self.service.get('run',second['id'])
        project=self.client.post('/api/projects',json={'name':'별도 실험','category':'ball-detection'},headers=self.headers).json()
        self.client.post('/api/pinocchios/'+second['pinocchio_id']+'/project',json={'project_id':project['id']},headers=self.headers)
        with self.assertRaisesRegex(ValueError,'one project'):self.service.compare([first['id'],second['id']])
