import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest

import httpx
from fastapi.testclient import TestClient

from functools import partial
from geppetto.app import create_app as _create_app
# These isolated fixtures exercise catalog behavior without browser authentication.
create_app = partial(_create_app, auth_required=False)
from geppetto.adapters import Airflow, Kubernetes, MLflow
from geppetto.configuration import digest
from geppetto.demo import tick
from geppetto.files import open_beneath
from geppetto.service import next_time, Service
from geppetto.storage import Store

HEADERS={'X-Geppetto-Request':'1'}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.home=Path(self.tmp.name)
        self.app=create_app(self.home,'demo',start_worker=False)
        self.service=self.app.state.service
        self.client=TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def body(self):
        body={'name':'test run','template_id':'ball-p2-v1','dataset_ids':['demo-ball-detection'],
              'overrides':{'epochs':2},'gpu':{'mode':'auto','count':1}}
        body['confirmation_hash']=self.service.preview(body)['confirmation_hash']
        return body

    def submit(self,body=None,key='unique-request-1'):
        return self.client.post('/api/submissions',json=body or self.body(),headers={**HEADERS,'Idempotency-Key':key})

    def test_idempotency_and_payload_conflict_survive_restart(self):
        body=self.body()
        a=self.submit(body).json()
        again=Service(Store(self.home/'catalog.sqlite3'),'demo',self.service.roots)
        self.assertEqual(again.submit(body,'unique-request-1')['id'],a['id'])
        body['name']='different'
        self.assertEqual(self.submit(body).status_code,422)

    def test_final_confirmation_is_required(self):
        body=self.body()
        body['overrides']['epochs']=3
        self.assertEqual(self.submit(body).status_code,422)

    def test_schedule_change_requires_new_confirmation(self):
        body=self.body()
        body['schedule']={'kind':'cron','cron':'* * * * *','timezone':'UTC'}
        self.assertEqual(self.submit(body).status_code,422)

    def test_shared_label_source_is_content_checked(self):
        root=self.home/'demo-data'/'extra'
        root.mkdir()
        (root/'image.png').write_bytes(b'image-fixture')
        (root/'track.csv').write_text('id,label\n1,7\n')
        dataset=self.service.register_dataset({'category':'jersey-recognition','name':'shared labels',
            'root':str(root),'samples':[{'image':'image.png','split':'train'}],'source_files':['track.csv']})
        from geppetto.files import verify_manifest
        verify_manifest(dataset)
        (root/'track.csv').write_text('id,label\n1,8\n')
        with self.assertRaises(ValueError):
            verify_manifest(dataset)

    def test_lifecycle_completion_and_verified_artifact(self):
        run=self.submit().json()
        for _ in range(13):
            tick(self.service,self.home)
        run=self.service.get('run',run['id'])
        self.assertEqual(run['state'],'succeeded')
        response=self.client.get(f'/api/runs/{run["id"]}/artifacts/0')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['epochs'],2)
        artifact=run['artifacts'][0]
        (Path(artifact['root'])/artifact['path']).write_text('changed')
        self.assertEqual(self.client.get(f'/api/runs/{run["id"]}/artifacts/0').status_code,409)

    def test_data_mutation_blocks_start(self):
        run=self.submit().json()
        dataset=self.service.get('dataset','demo-ball-detection')
        (Path(dataset['root'])/'0.json').write_text('{}')
        tick(self.service,self.home)
        self.assertEqual(self.service.get('run',run['id'])['state'],'failed')

    def test_stop_intent_persists_and_returns_resource(self):
        run=self.submit().json()
        tick(self.service,self.home)
        bad=self.client.post(f'/api/runs/{run["id"]}/stop',json={'name':'wrong'},headers=HEADERS)
        self.assertEqual(bad.status_code,422)
        self.service.stop(run['id'],run['name'])
        restarted=Service(Store(self.home/'catalog.sqlite3'),'demo',self.service.roots)
        tick(restarted,self.home)
        stopped=restarted.get('run',run['id'])
        self.assertEqual(stopped['state'],'stopped')
        self.assertTrue(stopped['resource_released'])

    def test_gpu_queue_does_not_switch_uuid(self):
        body=self.body()
        body['gpu']={'mode':'uuid','uuid':'GPU-DEMO-A','count':1}
        body['confirmation_hash']=self.service.preview(body)['confirmation_hash']
        a=self.submit(body,'gpu-request-a').json()
        b=self.submit(body,'gpu-request-b').json()
        self.service.concurrency=2
        tick(self.service,self.home)
        self.assertEqual(self.service.get('run',a['id'])['allocated_gpu'],'GPU-DEMO-A')
        self.assertEqual(self.service.get('run',b['id'])['state'],'queued')

    def test_schedule_transaction_and_overlap(self):
        body=self.body()
        body['schedule']={'kind':'cron','cron':'* * * * *','timezone':'Asia/Seoul'}
        body['confirmation_hash']=self.service.preview(body)['confirmation_hash']
        schedule=self.submit(body).json()
        now=schedule['next_at']+1
        with self.service.store.connect() as db:
            self.service.tick_schedules(db,now)
            self.service.tick_schedules(db,now)
        matches=[r for r in self.service.list('run')['items'] if r.get('schedule_id')==schedule['id']]
        self.assertEqual(len(matches),1)
        with self.service.store.connect() as db:
            self.service.tick_schedules(db,now+60)
        updated=self.service.get('schedule',schedule['id'])
        self.assertEqual(updated['history'][-1]['result'],'skipped_overlap')

    def test_cancelled_schedule_cannot_reactivate(self):
        body=self.body()
        body['schedule']={'kind':'cron','cron':'0 9 * * *','timezone':'UTC'}
        body['confirmation_hash']=self.service.preview(body)['confirmation_hash']
        schedule=self.submit(body).json()
        self.service.edit_schedule(schedule['id'],{'cancel':True})
        with self.assertRaises(ValueError):
            self.service.edit_schedule(schedule['id'],{'enabled':True})

    def test_dependency_failure_never_starts_child(self):
        parent=self.submit().json()
        self.service.stop(parent['id'],parent['name'])
        tick(self.service,self.home)
        body=self.body()
        body['schedule']={'kind':'after','run_id':parent['id']}
        body['confirmation_hash']=self.service.preview(body)['confirmation_hash']
        child=self.submit(body,'child-request-01').json()
        tick(self.service,self.home)
        self.assertEqual(self.service.get('schedule',child['id'])['history'][-1]['result'],'dependency_failed')

    def test_resume_not_confused_with_initialization(self):
        parent=self.submit().json()
        body=self.body()
        body.update(parent_id=parent['id'],relation='resume')
        with self.assertRaises(ValueError):
            self.service.preview(body)

    def test_comparison_preserves_zero_and_missing(self):
        rows=self.service.list('run',category='ball-detection')['items']
        a,b=rows[:2]
        a['metrics']['zero']=[{'step':0,'value':0}]
        with self.service.store.connect() as db:
            self.service.store.put(db,'run',a)
        result=self.service.compare([a['id'],b['id']])
        self.assertEqual(result['runs'][0]['metrics']['zero'][0]['value'],0)
        self.assertNotIn('zero',result['runs'][1]['metrics'])
        jersey=self.service.list('run',category='jersey-recognition')['items'][0]
        with self.assertRaises(ValueError):
            self.service.compare([a['id'],jersey['id']])

    def test_auth_origin_host_and_readonly(self):
        secure=TestClient(create_app(self.home,'readonly',False,token='secret-value'))
        self.assertEqual(secure.get('/api/status').status_code,401)
        self.assertEqual(secure.post('/api/auth/setup',json={'username':'admin','password':'test-password-123','setup_key':(self.home/'initial-admin-key').read_text()},headers=HEADERS).status_code,200)
        self.assertEqual(secure.get('/api/status').status_code,200)
        self.assertEqual(secure.post('/api/preview',json=self.body(),headers=HEADERS).status_code,422)
        self.assertEqual(self.client.post('/api/preview',json=self.body(),headers={**HEADERS,'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.client.get('/api/status',headers={'Host':'evil.example'}).status_code,403)
        secure.close()

    def test_pagination(self):
        result=self.client.get('/api/catalog/run?limit=1&offset=1').json()
        self.assertEqual(len(result['items']),1)
        self.assertEqual(result['total'],4)


class AdapterTests(unittest.TestCase):
    def test_airflow_conflict_reads_existing_run(self):
        requests=[]
        def handler(req):
            requests.append(req)
            return httpx.Response(409 if req.method=='POST' else 200,json={'state':'queued'})
        client=Airflow('http://example.test','token','geppetto_train',httpx.MockTransport(handler))
        self.assertEqual(client.submit({'id':'a','frozen':{},'name':'x'})['state'],'queued')
        self.assertEqual([r.method for r in requests],['POST','GET'])
        client.client.close()

    def test_kubernetes_delete_uses_uid_and_exact_submission(self):
        deleted=[]
        def handler(req):
            if req.method=='DELETE':
                deleted.append(json.loads(req.content))
                return httpx.Response(200,json={})
            return httpx.Response(200,json={'items':[] if deleted else [
                {'metadata':{'name':'own','uid':'uid-1','labels':{'geppetto-submission':'a'}}},
                {'metadata':{'name':'other','uid':'uid-2','labels':{'geppetto-submission':'b'}}}]})
        client=Kubernetes('http://example.test','token','isolated',transport=httpx.MockTransport(handler))
        self.assertTrue(client.stop('a'))
        self.assertEqual(len(deleted),1)
        self.assertEqual(deleted[0]['preconditions'],{'uid':'uid-1'})
        client.client.close()

    def test_metric_history_pagination(self):
        def handler(req):
            last='page_token' in req.url.params
            return httpx.Response(200,json={'metrics':[{'step':1 if last else 0,'timestamp':1000,'value':0}],
                                             **({} if last else {'next_page_token':'next'})})
        client=MLflow('http://example.test',transport=httpx.MockTransport(handler))
        self.assertEqual(len(client.history('r','loss')),2)
        client.client.close()

    def test_timezone_conversion(self):
        base=datetime(2026,1,1,0,0,tzinfo=timezone.utc).timestamp()
        nxt=next_time('0 9 * * *','Asia/Seoul',base)
        self.assertEqual(nxt-base,86400)


if __name__=='__main__':
    unittest.main()
