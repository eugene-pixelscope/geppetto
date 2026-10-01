import copy
from pathlib import Path
import tempfile
import unittest
from fastapi.testclient import TestClient
from geppetto.app import create_app

HEADERS={'X-Geppetto-Request':'1','Authorization':'Bearer history-test'}


class RunHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.home=Path(self.tmp.name)
        self.app=create_app(self.home,'catalog',False,token='history-test')
        self.store=self.app.state.service.store
        self.client=TestClient(self.app,headers=HEADERS)
        self.file=self.home/'weights.pt';self.file.write_bytes(b'untouched-checkpoint')
        self.rows=[{'id':id,'state':state,'name':id,'category':'ball-detection','frozen':{},
                    'mlflow':[],'airflow':{},'artifacts':[{'path':str(self.file)}]} for id,state in
                   [('complete','succeeded'),('failed','failed'),('active','running')]]
        with self.store.connect() as db:
            for row in self.rows:self.store.put(db,'run',row)

    def tearDown(self):
        self.client.close();self.tmp.cleanup()

    def change(self,ids,action='delete',client=None):
        return (client or self.client).post('/api/run-history/'+action,json={'ids':ids,'confirmation':action})

    def test_delete_restore_preserves_data_and_pagination(self):
        r=self.change(['complete']);self.assertEqual(r.status_code,200,r.text)
        self.assertFalse(r.json()['files_deleted'])
        result=self.client.get('/api/catalog/run?limit=1').json()
        self.assertEqual(result['total'],2);self.assertEqual(len(result['items']),1)
        deleted=self.client.get('/api/catalog/run?visibility=deleted').json()
        self.assertEqual([r['id'] for r in deleted['items']],['complete'])
        self.assertEqual(deleted['items'][0]['history_deletion']['actor_id'],'service-token')
        self.assertTrue(self.client.get('/api/catalog/run/complete').json()['history_deletion'])
        with self.store.connect() as db:self.assertEqual(self.store.get(db,'run','complete'),self.rows[0])
        self.assertEqual(self.file.read_bytes(),b'untouched-checkpoint')
        self.assertEqual(self.app.state.service.list('run')['total'],3)
        self.assertEqual(self.change(['complete']).json()['changed'],[])
        self.assertEqual(self.change(['complete'],'restore').status_code,200)
        self.assertEqual(self.client.get('/api/catalog/run').json()['total'],3)
        self.assertEqual(self.client.get('/api/catalog/run?visibility=deleted').json()['total'],0)
        self.assertEqual(self.change(['complete'],'restore').json()['changed'],[])

    def test_atomic_rejection_for_active_unknown_and_missing(self):
        self.assertEqual(self.change(['complete','active']).status_code,409)
        self.assertEqual(self.change(['complete','missing']).status_code,404)
        self.assertEqual(self.client.get('/api/catalog/run').json()['total'],3)
        with self.store.connect() as db:
            row=copy.deepcopy(self.rows[2]);row['state']='unknown';self.store.put(db,'run',row)
        self.assertEqual(self.change(['active']).status_code,409)

    def test_agent_resource_release_required(self):
        row=copy.deepcopy(self.rows[0]);row['frozen']={'execution':{'backend':'agent'}};row['dispatch']={'job_uid':'uid'}
        with self.store.connect() as db:self.store.put(db,'run',row)
        self.assertEqual(self.change(['complete']).status_code,409)
        row['resource_released']=True
        with self.store.connect() as db:self.store.put(db,'run',row)
        self.assertEqual(self.change(['complete']).status_code,200)

    def test_sync_and_reopened_runs(self):
        self.change(['complete'])
        # Simulate an import replacing the entire run document.
        with self.store.connect() as db:self.store.put(db,'run',self.rows[0])
        self.assertEqual(self.client.get('/api/catalog/run?visibility=deleted').json()['total'],1)
        with self.store.connect() as db:
            run=copy.deepcopy(self.rows[0]);run['state']='running';self.store.put(db,'run',run)
        self.assertEqual(self.client.get('/api/catalog/run').json()['total'],3)
        self.assertEqual(self.client.get('/api/catalog/run?visibility=deleted').json()['total'],0)

    def test_auth_readonly_validation(self):
        anonymous=TestClient(self.app,headers={"X-Geppetto-Request":"1"})
        self.assertEqual(self.change(['complete'],client=anonymous).status_code,401)
        self.assertEqual(self.client.post('/api/run-history/delete',json={'ids':['complete']}).status_code,422)
        for ids in ([],['complete','complete'],[{}],['x']*101):
            self.assertEqual(self.change(ids).status_code,422)
        self.assertEqual(self.client.get('/api/catalog/run?visibility=invalid').status_code,422)
        readonly=TestClient(create_app(self.home,'readonly',False,token='history-test'),headers=HEADERS)
        self.assertEqual(self.change(['complete'],client=readonly).status_code,403)
        self.assertEqual(self.change(['complete'],'restore',readonly).status_code,403)
        readonly.close();anonymous.close()

    def test_persistence_and_all_visibility(self):
        self.change(['complete','failed'])
        restarted=TestClient(create_app(self.home,'catalog',False,token='history-test'),headers=HEADERS)
        self.assertEqual(restarted.get('/api/catalog/run?visibility=deleted').json()['total'],2)
        self.assertEqual(restarted.get('/api/catalog/run?visibility=all').json()['total'],3)
        self.assertEqual(restarted.get('/api/catalog/run?visibility=deleted&category=other').json()['total'],0)
        restarted.close()


if __name__=='__main__':unittest.main()
