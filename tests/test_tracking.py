import base64
import hashlib
from pathlib import Path
import tempfile
import unittest
import uuid
from fastapi.testclient import TestClient
from functools import partial
from geppetto.app import create_app as _create_app
# These isolated fixtures exercise catalog behavior without browser authentication.
create_app = partial(_create_app, auth_required=False)

class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=create_app(Path(self.tmp.name),'catalog',False,token='operator')
        self.s=self.app.state.service;self.c=TestClient(self.app)
        self.operator={'Authorization':'Bearer operator','X-Geppetto-Request':'1'}
        self.key=self.issue('project-ball-detection')
    def tearDown(self):self.c.close();self.tmp.cleanup()
    def issue(self,project):
        r=self.c.post('/api/sdk/tokens',headers=self.operator,json={'project_id':project})
        self.assertEqual(r.status_code,200,r.text);return r.json()
    def post(self,path,body,key=None):
        return self.c.post('/api/tracking/'+path,headers={'Authorization':'Bearer '+(key or self.key)['token'],'X-Geppetto-Request':'1'},json={'request_id':uuid.uuid4().hex,**body})
    def init(self,**kwargs):
        r=self.post('runs',dict(name='YOLO26 P2',config={'epochs':2},**kwargs));self.assertEqual(r.status_code,200,r.text);return r.json()
    def event(self,run,**kwargs):return self.post('runs/'+run['id']+'/events',kwargs)
    def test_auth_scope_revocation(self):
        self.assertEqual(self.c.post('/api/tracking/runs',json={},headers={'X-Geppetto-Request':'1'}).status_code,422)
        self.assertEqual(self.post('runs',{'project':'wrong'}).status_code,403)
        run=self.init();other=self.issue('project-jersey-recognition')
        self.assertEqual(self.post('runs/'+run['id']+'/events',{'kind':'heartbeat'},other).status_code,403)
        self.assertEqual(self.c.get('/api/status',headers={'Authorization':'Bearer '+self.key['token']}).status_code,401)
        listed=self.c.get('/api/sdk/tokens',headers=self.operator).json();self.assertNotIn('token_hash',str(listed));self.assertNotIn(self.key['token'],str(listed))
        self.c.post('/api/sdk/tokens/'+self.key['id']+'/revoke',headers=self.operator,json={})
        self.assertEqual(self.event(run,kind='heartbeat').status_code,401)
    def test_idempotency_and_epoch_metrics(self):
        body={'request_id':uuid.uuid4().hex,'name':'YOLO26 P2'}
        a=self.post('runs',body);b=self.post('runs',body);self.assertEqual(a.json(),b.json())
        self.assertEqual(self.post('runs',{**body,'name':'changed'}).status_code,422)
        run=a.json();event=dict(kind='log',step=1,epoch=1,metrics={'train/loss':.4},request_id=uuid.uuid4().hex)
        self.assertEqual(self.event(run,**event).status_code,200);self.assertEqual(self.event(run,**event).status_code,200)
        self.assertEqual(self.event(run,**{**event,'metrics':{'train/loss':.3}}).status_code,422)
        self.assertEqual(self.event(run,kind='log',step=0,metrics={'a':1}).status_code,422)
        self.assertEqual(len(self.s.get('run',run['id'])['metrics']['train/loss']),1)
        self.assertEqual(self.event(run,kind='progress',epoch=2,total_epochs=2).status_code,200)
        self.assertEqual(self.event(run,kind='log',step=2,epoch=1,metrics={'a':1}).status_code,422)
        self.assertEqual(self.event(run,kind='finish').status_code,200)
        self.assertEqual(self.event(run,kind='log',step=3,metrics={'a':1}).status_code,422)
    def test_model_identity_resume_evaluate(self):
        a=self.init();b=self.init();self.assertNotEqual(a['model_id'],b['model_id'])
        self.assertEqual(self.post('runs',{'job_type':'resume','parent_run_id':a['id']}).status_code,422)
        self.event(a,kind='finish',state='failed')
        r=self.post('runs',{'job_type':'resume','parent_run_id':a['id']});self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['model_id'],a['model_id'])
        self.assertEqual(self.post('runs',{'job_type':'resume','parent_run_id':a['id'],'config':{}}).status_code,422)
        self.event(r.json(),kind='finish')
        e=self.post('runs',{'job_type':'evaluate','parent_run_id':r.json()['id']});self.assertEqual(e.status_code,200,e.text);self.assertEqual(e.json()['model_id'],a['model_id'])
        self.c.get('/api/workshop',headers=self.operator)
        self.assertEqual(self.s.list('pinocchio')['total'],2)
    def test_artifact_integrity_retries_and_download(self):
        run=self.init();key=uuid.uuid4().hex;data=b'checkpoint bytes';sha=hashlib.sha256(data).hexdigest();path='runs/'+run['id']+'/artifacts'
        start={'request_id':key,'action':'start','name':'best.pt','size':len(data),'sha256':sha}
        self.assertEqual(self.post(path,{**start,'name':'../escape.pt'}).status_code,422)
        self.assertEqual(self.post(path,start).status_code,200)
        self.assertEqual(self.post(path,{'request_id':key,'action':'complete'}).status_code,422)
        chunk={'request_id':key,'action':'chunk','offset':0,'data':base64.b64encode(data).decode()}
        self.assertEqual(self.post(path,chunk).status_code,200);self.assertEqual(self.post(path,chunk).status_code,200)
        self.assertEqual(self.post(path,{**chunk,'data':base64.b64encode(b'x'*len(data)).decode()}).status_code,422)
        self.assertEqual(self.post(path,{'request_id':key,'action':'complete'}).status_code,200)
        self.assertEqual(self.post(path,{'request_id':key,'action':'complete'}).status_code,200)
        self.assertEqual(len(self.s.get('run',run['id'])['artifacts']),1)
        self.event(run,kind='finish')
        download=self.c.get('/api/runs/'+run['id']+'/artifacts/'+key,headers=self.operator)
        self.assertEqual(download.status_code,200,download.text);self.assertEqual(download.content,data)
    def test_partial_bad_hash_is_not_published(self):
        run=self.init();key=uuid.uuid4().hex;path='runs/'+run['id']+'/artifacts'
        self.post(path,dict(request_id=key,action='start',name='bad.pt',size=1,sha256='0'*64))
        self.post(path,dict(request_id=key,action='chunk',offset=0,data='YQ=='))
        self.assertEqual(self.post(path,dict(request_id=key,action='complete')).status_code,422)
        self.assertEqual(self.s.get('run',run['id'])['artifacts'],[])
