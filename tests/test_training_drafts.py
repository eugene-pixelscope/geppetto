from pathlib import Path
import tempfile
import unittest
from fastapi.testclient import TestClient
from functools import partial
from geppetto.app import create_app as _create_app
# These isolated fixtures exercise catalog behavior without browser authentication.
create_app = partial(_create_app, auth_required=False)
from geppetto.workshop import reconcile

class TrainingDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=create_app(Path(self.tmp.name),'catalog',False)
        self.service=self.app.state.service;reconcile(self.service.store)
        with self.service.store.connect() as db:
            self.service.store.put(db,'dataset',{'id':'real-data','name':'Ball','category':'ball-detection','node_id':'whale-local','version':'abc','content_verified':False})
        self.client=TestClient(self.app);self.headers={'X-Geppetto-Request':'1'}
        self.body={'name':'YOLO26 P2 run','node_id':'whale-local','project_id':'project-ball-detection','architecture_id':'yolo26-p2-pose','dataset_id':'real-data','parameters':{'epochs':245,'batch':16,'imgsz':1920,'learning_rate':.0054,'patience':100}}
    def tearDown(self):self.client.close();self.tmp.cleanup()
    def test_save_edit_reopen_never_creates_run(self):
        preview=self.client.post('/api/training/preview',json=self.body,headers=self.headers);self.assertEqual(preview.status_code,200)
        self.assertFalse(preview.json()['execution_ready']);self.assertEqual(self.service.list('training_draft')['total'],0)
        saved=self.client.post('/api/training/drafts',json=self.body,headers=self.headers).json()
        changed={**self.body,'id':saved['id'],'parameters':{**self.body['parameters'],'epochs':100}}
        self.assertEqual(self.client.post('/api/training/drafts',json=changed,headers=self.headers).status_code,200)
        options=self.client.get('/api/training/options').json();self.assertEqual(len(options['drafts']),1)
        self.assertEqual(options['drafts'][0]['parameters']['epochs'],100)
        self.assertEqual(self.service.list('run')['total'],0);self.assertEqual(self.service.list('pinocchio')['total'],0)
    def test_invalid_parameters_and_category(self):
        for key,value in [('epochs',0),('batch',-1),('imgsz',1930),('learning_rate',0),('epochs',True)]:
            body={**self.body,'parameters':{**self.body['parameters'],key:value}}
            self.assertEqual(self.client.post('/api/training/preview',json=body,headers=self.headers).status_code,422)
        body={**self.body,'architecture_id':'jersey-vit'}
        self.assertEqual(self.client.post('/api/training/preview',json=body,headers=self.headers).status_code,422)
        self.assertEqual(self.service.list('run')['total'],0)

    def test_multiple_dataset_draft_and_version_pinning(self):
        with self.service.store.connect() as db:
            self.service.store.put(db,'dataset',{'id':'second','name':'More ball','category':'ball-detection','node_id':'whale-local','version':'def','splits':{'train':3}})
        body={**self.body,'dataset_ids':['real-data','second']}
        saved=self.client.post('/api/training/drafts',json=body,headers=self.headers).json()
        self.assertEqual(saved['dataset_ids'],['real-data','second'])
        self.assertEqual(saved['dataset_versions'],{'real-data':'abc','second':'def'})
        for ids in ([],['real-data','real-data']):
            self.assertEqual(self.client.post('/api/training/preview',json={**body,'dataset_ids':ids},headers=self.headers).status_code,422)
        changed={**body,'dataset_versions':{'real-data':'old','second':'def'}}
        self.assertEqual(self.client.post('/api/training/preview',json=changed,headers=self.headers).status_code,422)

    def test_jersey_selection_is_exported_and_requires_validation(self):
        options=self.client.get('/api/training/options').json()
        architecture=next(a for a in options['architectures'] if a['id']=='jersey-resnet18')
        with self.service.store.connect() as db:
            for name,split in [('jersey-a','train'),('jersey-b','train'),('jersey-v','validation'),('jersey-t','test')]:
                self.service.store.put(db,'dataset',{'id':name,'name':name,'node_id':'whale-local','category':'jersey-recognition',
                    'root':'/data','version':name,'splits':{split:1},'policy':{'format':'jersey-number'},'content_verified':True,
                    'local_manifest':'/prepared/'+name+'.json','local_manifest_version':name,
                    'samples':[{'image':name+'.png','split':split,'number':7,'image_file':{'sha256':name}}]})
        body={**self.body,'project_id':'project-jersey-recognition','architecture_id':'jersey-resnet18',
              'parameters':architecture['defaults'],'dataset_ids':['jersey-a','jersey-b','jersey-v']}
        response=self.client.post('/api/training/preview',json=body,headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        config=response.json()['training_config']
        self.assertEqual(len(config['manifests']),3)
        self.assertEqual(config['parameters']['sampling'],'balanced-source')
        self.assertEqual(response.json()['splits'],{'train':2,'validation':1,'test':0})
        for ids in (['jersey-a'],['jersey-a','jersey-t']):
            self.assertEqual(self.client.post('/api/training/preview',json={**body,'dataset_ids':ids},headers=self.headers).status_code,422)
        with self.service.store.connect() as db:
            copied=self.service.store.get(db,'dataset','jersey-v');copied['samples'][0]['image_file']['sha256']='jersey-a'
            self.service.store.put(db,'dataset',copied)
        self.assertEqual(self.client.post('/api/training/preview',json=body,headers=self.headers).status_code,422)
