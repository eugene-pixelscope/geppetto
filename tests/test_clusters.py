import copy
import json
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
from functools import partial
from geppetto.app import create_app as _create_app
# These isolated fixtures exercise catalog behavior without browser authentication.
create_app = partial(_create_app, auth_required=False)
from geppetto.agent import identity,cycle,KubernetesInventory,validate_server
from geppetto.clusters import public
from geppetto.cleanup import clean_synthetic

OP={'X-Geppetto-Request':'1','Authorization':'Bearer operator-secret'}
NODE={'uid':'node-uid','name':'gpu-1','ready':'True','unschedulable':False,'capacity':{'cpu':'32','memory':'64Gi','nvidia.com/gpu':'2'},'allocatable':{'cpu':'30','memory':'60Gi','nvidia.com/gpu':'2'},'kubelet_version':'v1.test'}

class ClusterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.home=Path(self.tmp.name)
        self.app=create_app(self.home,'catalog',False,token='operator-secret');self.store=self.app.state.service.store
        self.client=TestClient(self.app)
        self.c=self.client.post('/api/clusters',json={'name':'fixture'},headers=OP).json()
        self.body={'cluster_id':self.c['id'],'cluster_uid':'cluster-uid','agent_id':'agent-id','agent_token':secrets.token_urlsafe(48),'version':'test'}
        self.boot={'X-Geppetto-Request':'1','Authorization':'Bearer '+self.c['bootstrap_token']}
        self.auth={'X-Geppetto-Request':'1','Authorization':'Bearer '+self.body['agent_token']}
    def tearDown(self):self.client.close();self.tmp.cleanup()
    def enroll(self):
        r=self.client.post('/api/agent/enroll',json=self.body,headers=self.boot)
        self.assertEqual(r.status_code,200,r.text);return r
    def approve(self):self.client.post('/api/clusters/'+self.c['id']+'/approve',json={},headers=OP)
    def snap(self,sequence=1,nodes=None):
        return {'cluster_id':self.c['id'],'cluster_uid':'cluster-uid','agent_id':'agent-id','sequence':sequence,'nodes':[NODE] if nodes is None else nodes,'pvcs':[]}
    def test_credentials_scope_approval_and_replay(self):
        self.assertEqual(self.client.get('/api/clusters').status_code,401)
        self.assertEqual(self.client.post('/api/agent/enroll',json=self.body,headers=OP).status_code,401)
        self.enroll();self.enroll()  # lost response retry is idempotent
        self.assertEqual(self.client.post('/api/agent/snapshot',json=self.snap(),headers=self.auth).status_code,403)
        self.approve()
        first=self.client.post('/api/agent/snapshot',json=self.snap(),headers=self.auth)
        self.assertEqual(first.status_code,200,first.text)
        self.assertTrue(self.client.post('/api/agent/snapshot',json=self.snap(),headers=self.auth).json()['duplicate'])
        changed=self.snap();changed['nodes']=[]
        self.assertEqual(self.client.post('/api/agent/snapshot',json=changed,headers=self.auth).status_code,409)
        listing=self.client.get('/api/clusters',headers=OP).json()
        rendered=json.dumps(listing)
        for secret in (self.c['bootstrap_token'],self.body['agent_token'],'agent_hash','bootstrap_hash'):self.assertNotIn(secret,rendered)
        self.assertEqual(listing['items'][0]['state'],'connected')
        self.assertEqual(self.client.get('/api/clusters',headers=self.auth).status_code,401)
    def test_uid_replacement_missing_nodes_and_degraded_snapshot(self):
        self.enroll();self.approve();self.client.post('/api/agent/snapshot',json=self.snap(),headers=self.auth)
        url='/api/clusters/'+self.c['id']+'/adopt'
        first=self.client.post(url,json={'node_uid':'node-uid'},headers=OP).json()
        self.assertEqual(self.client.post(url,json={'node_uid':'node-uid'},headers=OP).json()['id'],first['id'])
        err={**self.snap(2),'error':'Kubernetes unavailable'}
        self.client.post('/api/agent/snapshot',json=err,headers=self.auth)
        self.assertEqual(self.client.post(url,json={'node_uid':'node-uid'},headers=OP).status_code,422)
        with self.store.connect() as db:
            self.assertTrue(self.store.get(db,'whale',first['id'])['present'])
        second=copy.deepcopy(NODE);second['uid']='replacement'
        self.client.post('/api/agent/snapshot',json=self.snap(3,[second]),headers=self.auth)
        new=self.client.post(url,json={'node_uid':'replacement'},headers=OP).json()
        self.assertNotEqual(new['id'],first['id'])
        with self.store.connect() as db:self.assertFalse(self.store.get(db,'whale',first['id'])['present'])
    def test_expiry_rotation_revocation_and_manifest(self):
        response=self.client.post('/api/clusters/'+self.c['id']+'/manifest',json={'server_url':'https://central.example','bootstrap_token':self.c['bootstrap_token']},headers=OP)
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn(self.c['id'],response.json()['manifest'])
        self.assertNotIn('cluster-admin',response.json()['manifest'])
        self.enroll();self.approve()
        self.client.post('/api/clusters/'+self.c['id']+'/revoke',json={},headers=OP)
        self.assertEqual(self.client.post('/api/agent/snapshot',json=self.snap(),headers=self.auth).status_code,401)
        self.client.post('/api/clusters/'+self.c['id']+'/rotate',json={},headers=OP)
        self.assertEqual(self.client.post('/api/agent/enroll',json=self.body,headers=self.boot).status_code,401)
        with self.store.connect() as db:
            c=self.store.get(db,'cluster',self.c['id']);c['bootstrap_expires_at']=0;self.store.put(db,'cluster',c)
        self.assertEqual(self.client.get('/api/clusters',headers=OP).json()['items'][0]['state'],'registration_expired')
    def test_agent_real_cycle_with_mock_kubernetes_and_restart(self):
        token=self.home/'sa-token';token.write_text('rotating-sa-token')
        def handler(req):
            self.assertEqual(req.headers['authorization'],'Bearer rotating-sa-token')
            if req.url.path.endswith('kube-system'):return httpx.Response(200,json={'metadata':{'uid':'cluster-uid'}})
            if req.url.path.endswith('/nodes'):
                if 'continue' not in req.url.params:return httpx.Response(200,json={'items':[],'metadata':{'continue':'next'}})
                return httpx.Response(200,json={'items':[{'metadata':{'uid':NODE['uid'],'name':NODE['name']},'status':{'conditions':[{'type':'Ready','status':'True'}],'capacity':NODE['capacity'],'allocatable':NODE['allocatable']}}]})
            return httpx.Response(200,json={'items':[]})
        with httpx.Client(transport=httpx.MockTransport(handler),base_url='https://kube.test') as http:
            kube=KubernetesInventory(http,token,'geppetto-agent');path=self.home/'identity.json'
            state=identity(path,self.c['id'],self.c['bootstrap_token'])
            # Agent is enrolled but cannot send inventory before operator approval.
            with self.assertRaises(httpx.HTTPStatusError):cycle(self.client,kube,state,path,self.c['bootstrap_token'])
            self.approve()
            state=identity(path,self.c['id'],self.c['bootstrap_token'])
            cycle(self.client,kube,state,path,self.c['bootstrap_token'])
            c=self.client.get('/api/clusters',headers=OP).json()['items'][0]
            self.assertEqual(len(c['nodes']),1)
            self.assertEqual(c['state'],'connected')
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        with self.store.connect() as db:
            raw=self.store.get(db,'cluster',self.c['id'])
        self.assertEqual(public(raw,raw['last_seen_at']+70)['state'],'stale')
        self.assertEqual(public(raw,raw['last_seen_at']+181)['state'],'disconnected')
        with self.assertRaises(ValueError):validate_server('http://remote.example')

    def test_custom_whale_name_survives_agent_inventory(self):
        self.enroll();self.approve();self.client.post('/api/agent/snapshot',json=self.snap(),headers=self.auth)
        whale=self.client.post('/api/clusters/'+self.c['id']+'/adopt',json={'node_uid':NODE['uid']},headers=OP).json()
        result=self.client.patch('/api/whales/'+whale['id'],json={'name':'IDC'},headers=OP)
        self.assertEqual(result.status_code,200)
        self.client.post('/api/agent/snapshot',json=self.snap(2),headers=self.auth)
        current=next(w for w in self.client.get('/api/workshop',headers=OP).json()['whale'] if w['id']==whale['id'])
        self.assertEqual(current['name'],'IDC')
        self.assertEqual(current['host'],NODE['name'])


class CleanupTests(unittest.TestCase):
    def test_preserves_real_history_and_disables_demo_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory);app=create_app(home,'demo',False);s=app.state.service;store=s.store
            with store.connect() as db:
                source=next(r for r in store.all(db,'run') if r['category']=='ball-detection')
                real=copy.deepcopy(source);real.update(id='real-job',synthetic=False,imported=True)
                store.put(db,'run',real)
            removed=clean_synthetic(store)
            self.assertEqual(removed['run'],4)
            again=create_app(home,'demo',False)
            self.assertEqual(again.state.service.mode,'catalog')
            with store.connect() as db:
                self.assertEqual([r['id'] for r in store.all(db,'run')],['real-job'])
                self.assertEqual(store.get(db,'pinocchio',real['pinocchio_id'])['id'],real['pinocchio_id'])
                self.assertTrue(store.get(db,'dataset','demo-ball-detection'))
            with self.assertRaises(ValueError):again.state.service.preview({'name':'no'})

