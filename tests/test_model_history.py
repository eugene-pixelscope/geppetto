from pathlib import Path
import tempfile
import unittest
from fastapi.testclient import TestClient
from geppetto.app import create_app
from geppetto.workshop import topology
from geppetto.model_history import ensure_visible
from fastapi import HTTPException

H={'X-Geppetto-Request':'1','Authorization':'Bearer model-test'}
class ModelHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=create_app(self.tmp.name,'demo',False,token='model-test')
        self.service=self.app.state.service;self.store=self.service.store
        topology(self.service)
        self.client=TestClient(self.app,headers=H)
        self.run=self.service.list('run')['items'][0]
        self.model=self.service.get('pinocchio',self.run['pinocchio_id'])
        with self.store.connect() as db:
            for r in self.store.all(db,'run'):
                if r.get('pinocchio_id')==self.model['id']:
                    r['state']='succeeded';self.store.put(db,'run',r)
            for s in self.store.all(db,'schedule'):
                if s.get('pinocchio_id')==self.model['id']:
                    s['enabled']=False;self.store.put(db,'schedule',s)
    def tearDown(self):self.client.close();self.tmp.cleanup()
    def change(self,action='delete',name=None):
        return self.client.post('/api/pinocchios/'+self.model['id']+'/'+action,json={'name':name or self.model['name']})
    def test_model_and_runs_permanently_deleted(self):
        self.assertEqual(self.change().status_code,200)
        for kind,id in [('pinocchio',self.model['id']),('run',self.run['id'])]:
            self.assertEqual(self.client.get('/api/catalog/'+kind+'/'+id).status_code,404)
            with self.assertRaises(KeyError):self.service.get(kind,id)
        self.assertNotIn(self.model['id'],[p['id'] for p in self.client.get('/api/workshop').json()['pinocchio']])
        self.assertEqual(self.change('restore').status_code,404)
        self.assertEqual(self.client.get('/api/model-history').status_code,404)
        with self.store.connect() as db:
            self.assertEqual(set(self.store.get(db,'model_history',self.model['id'])),{'id','deleted_at'})
            with self.assertRaises(HTTPException):ensure_visible(self.store,db,self.model['id'])
    def test_active_or_resource_unreleased_or_schedule_rejected(self):
        with self.store.connect() as db:
            run=self.store.get(db,'run',self.run['id']);run['state']='running';self.store.put(db,'run',run)
        self.assertEqual(self.change().status_code,409)
        with self.store.connect() as db:
            run['state']='failed';run['frozen']['execution']={'backend':'agent'};run['dispatch']={'job_uid':'uid'};run['resource_released']=False;self.store.put(db,'run',run)
        self.assertEqual(self.change().status_code,409)
        with self.store.connect() as db:
            run['resource_released']=True;self.store.put(db,'run',run)
            self.store.put(db,'schedule',{'id':'future','pinocchio_id':self.model['id'],'enabled':True})
        self.assertEqual(self.change().status_code,409)
        self.assertEqual(self.service.get('pinocchio',self.model['id'])['id'],self.model['id'])
    def test_related_records_removed(self):
        self.client.post('/api/run-history/delete',json={'ids':[self.run['id']],'confirmation':'delete'})
        with self.store.connect() as db:
            self.store.put(db,'evaluation',{'id':'report','run_id':self.run['id']})
        self.assertEqual(self.change().status_code,200)
        with self.store.connect() as db:
            self.assertFalse([d for d in self.store.all(db,'run_history') if d['id']==self.run['id']])
            self.assertFalse([d for d in self.store.all(db,'evaluation') if d['id']=='report'])
            self.assertFalse([d for d in self.store.all(db,'schedule') if d.get('pinocchio_id')==self.model['id']])
    def test_empty_model_and_name_validation(self):
        self.assertEqual(self.change(name='changed-name').status_code,409)
        with self.store.connect() as db:
            p={**self.model,'id':'empty','name':'empty'};self.store.put(db,'pinocchio',p)
        response=self.client.post('/api/pinocchios/empty/delete',json={'name':'empty'})
        self.assertEqual(response.status_code,200,response.text);self.assertEqual(response.json()['run_ids'],[])
    def test_readonly(self):
        client=TestClient(create_app(self.tmp.name,'readonly',False,token='model-test'),headers=H)
        self.assertEqual(client.post('/api/pinocchios/'+self.model['id']+'/delete',json={'name':self.model['name']}).status_code,403)
        client.close()
if __name__=='__main__':unittest.main()
