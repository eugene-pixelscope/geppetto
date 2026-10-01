import copy
import json
from pathlib import Path
import secrets
import tempfile
import time
import unittest
import httpx
from fastapi.testclient import TestClient
from functools import partial
from geppetto.app import create_app as _create_app
# These isolated fixtures exercise catalog behavior without browser authentication.
create_app = partial(_create_app, auth_required=False)
from geppetto.agent import KubernetesInventory
from geppetto.job_agent import JobAgent, cycle as job_cycle
from geppetto.execution import job_manifest
from geppetto.configuration import digest
from geppetto.files import verify_manifest
from geppetto.indexed_datasets import import_yolo_splits, sample_at
from geppetto.dataset_versions import tick, export_registry
from geppetto.workshop import reconcile

OP = {'X-Geppetto-Request': '1', 'Authorization': 'Bearer operator'}
IMAGE = 'registry.test/model@sha256:' + 'a'*64
NODE = {'uid': 'node-uid', 'name': 'gpu-one', 'ready': 'True', 'unschedulable': False,
        'capacity': {'nvidia.com/gpu': '2'}, 'allocatable': {'nvidia.com/gpu': '2'}}


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.app = create_app(self.home/'app', 'catalog', False, token='operator')
        self.service = self.app.state.service
        reconcile(self.service.store)
        self.client = TestClient(self.app)
        self.cluster = self.post('/api/clusters', {'name': 'independent-cluster'})
        self.ident = {'cluster_id': self.cluster['id'], 'cluster_uid': 'cluster-uid', 'agent_id': 'agent-one'}
        self.token = secrets.token_urlsafe(48)
        self.auth = {'X-Geppetto-Request': '1', 'Authorization': 'Bearer '+self.token}
        self.post('/api/agent/enroll', {**self.ident, 'agent_token': self.token, 'version': 'test'},
                  {'X-Geppetto-Request': '1', 'Authorization': 'Bearer '+self.cluster['bootstrap_token']})
        self.post('/api/clusters/'+self.cluster['id']+'/approve', {})
        self.post('/api/agent/snapshot', {**self.ident, 'sequence': 1, 'nodes': [NODE],
                  'pvcs': [{'uid': name+'-uid', 'name': name, 'namespace': 'training', 'phase': 'Bound'} for name in ('data', 'results')]}, self.auth)
        self.node = self.post('/api/clusters/'+self.cluster['id']+'/adopt', {'node_uid': NODE['uid']})
        self.profile_body = {'template_id': 'ball-p2-v1', 'image': IMAGE, 'namespace': 'training',
                             'datasets_pvc': 'data', 'results_pvc': 'results', 'mlflow_uri': 'http://mlflow:5000',
                             'enabled': True, 'concurrency': 2}
        self.profile = self.post('/api/whales/'+self.node['id']+'/execution', self.profile_body)
        root = self.home/'data';root.mkdir()
        (root/'image').write_bytes(b'fixture')
        self.service.roots = [str(root)]
        self.dataset = self.service.register_dataset({'name': 'data', 'category': 'ball-detection', 'root': str(root),
                  'samples': [{'image': 'image', 'split': 'validation'}]})
        self.post('/api/datasets/'+self.dataset['id']+'/locations', {'node_id': self.node['id'], 'namespace': 'training', 'pvc': 'data', 'root': 'ball'})
        self.request = {'name': 'independent-run', 'node_id': self.node['id'], 'template_id': 'ball-p2-v1',
                        'dataset_ids': [self.dataset['id']], 'overrides': {}, 'gpu': {'mode': 'auto', 'count': 1}}

    def tearDown(self):
        self.client.close();self.tmp.cleanup()

    def post(self, path, body, headers=None):
        r = self.client.post(path, json=body, headers=headers or OP)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def launch(self, suffix='default'):
        preview = self.post('/api/preview', self.request)
        return self.post('/api/submissions', {**self.request, 'confirmation_hash': preview['confirmation_hash']},
                         {**OP, 'Idempotency-Key': 'request-'+suffix})

    def poll(self):
        return self.post('/api/agent/jobs/poll', self.ident, self.auth)['commands']

    def observation(self, run, sequence=1, **kwargs):
        return {**self.ident, 'run_id': run['id'], 'sequence': sequence, 'job_uid': 'job-uid', 'state': 'running', **kwargs}

    def test_submission_replay_concurrency_and_frozen_resources(self):
        one = self.launch('one');two = self.launch('two');three = self.launch('three')
        self.assertEqual(self.launch('one')['id'], one['id'])
        commands = self.poll()
        self.assertEqual(len(commands), 2)
        self.assertEqual({c['id'] for c in self.poll()}, {one['id'], two['id']})
        self.assertEqual(self.service.get('run', three['id'])['state'], 'queued')
        manifest = job_manifest(commands[0]);pod = manifest['spec']['template']['spec']
        self.assertEqual(manifest['spec']['backoffLimit'], 0)
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(pod['containers'][0]['image'], IMAGE)
        self.assertNotIn('nodeName', pod)  # Keep Kubernetes resource scheduling.
        self.assertIn(NODE['name'], json.dumps(pod['affinity']))
        self.assertEqual(self.service.get('run', one['id'])['airflow'], [])

    def test_agent_auth_replay_disconnect_and_monotonic_terminal_state(self):
        run = self.launch();self.poll()
        self.assertEqual(self.client.post('/api/agent/jobs/poll', json=self.ident, headers=OP).status_code, 401)
        event = self.observation(run)
        self.post('/api/agent/jobs/observe', event, self.auth)
        self.assertTrue(self.post('/api/agent/jobs/observe', event, self.auth)['duplicate'])
        self.assertEqual(self.client.post('/api/agent/jobs/observe', json={**event, 'state': 'queued'}, headers=self.auth).status_code, 409)
        self.post('/api/agent/jobs/observe', self.observation(run, 2, state='unknown', error='temporary timeout'), self.auth)
        self.assertEqual(self.service.get('run', run['id'])['state'], 'running')
        self.assertEqual(self.client.post('/api/agent/jobs/observe', json=self.observation(run, 3, job_uid='replacement'), headers=self.auth).status_code, 409)
        self.post('/api/runs/'+run['id']+'/stop', {'name': run['name']})
        self.assertEqual(self.poll()[0]['action'], 'stop')
        bad = self.observation(run, 3, state='stopped', resource_released=False)
        self.assertEqual(self.client.post('/api/agent/jobs/observe', json=bad, headers=self.auth).status_code, 422)
        self.post('/api/agent/jobs/observe', {**bad, 'resource_released': True}, self.auth)
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.client.post('/api/agent/jobs/observe', json=self.observation(run, 4), headers=self.auth).status_code, 409)

    def test_cancel_before_dispatch_and_disable_profile(self):
        run = self.launch()
        self.post('/api/runs/'+run['id']+'/stop', {'name': run['name']})
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.service.get('run', run['id'])['state'], 'stopped')
        two = self.launch('two')
        self.post('/api/whales/'+self.node['id']+'/execution', {**self.profile_body, 'enabled': False})
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.service.get('run', two['id'])['state'], 'queued')

    def test_profile_and_dataset_constraints(self):
        for change in ({'image': 'registry/model:latest'}, {'results_pvc': 'data'}, {'datasets_pvc': 'absent'}, {'concurrency': 0}):
            self.assertEqual(self.client.post('/api/whales/'+self.node['id']+'/execution', json={**self.profile_body, **change}, headers=OP).status_code, 422)
        with self.service.store.connect() as db:
            d = self.service.store.get(db, 'dataset', self.dataset['id']);d.update(content_verified=False, status='indexed');self.service.store.put(db,'dataset',d)
        self.assertEqual(self.client.post('/api/preview', json=self.request, headers=OP).status_code, 422)

    def test_two_nodes_dispatch_independently_and_do_not_exceed_limits(self):
        second={**NODE,'uid':'node-two','name':'gpu-two'}
        self.post('/api/agent/snapshot',{**self.ident,'sequence':2,'nodes':[NODE,second],
            'pvcs':[{'uid':name+'-uid','name':name,'namespace':'training','phase':'Bound'} for name in ('data','results')]},self.auth)
        node=self.post('/api/clusters/'+self.cluster['id']+'/adopt',{'node_uid':second['uid']})
        self.post('/api/whales/'+self.node['id']+'/execution',{**self.profile_body,'concurrency':1})
        self.post('/api/whales/'+node['id']+'/execution',{**self.profile_body,'concurrency':1})
        self.post('/api/datasets/'+self.dataset['id']+'/locations',{'node_id':node['id'],'namespace':'training','pvc':'data','root':'ball'})
        first=self.launch('one');self.launch('waiting')
        self.request['node_id']=node['id'];other=self.launch('other')
        commands=self.poll()
        self.assertEqual({c['id'] for c in commands},{first['id'],other['id']})
        self.assertEqual({c['frozen']['execution']['profile']['node_name'] for c in commands},{NODE['name'],second['name']})

    def test_observations_cannot_publish_remote_local_paths(self):
        run=self.launch();self.poll()
        event=self.observation(run,state='succeeded',result={'submission_id':run['id'],'state':'succeeded','metrics':{'accuracy':[{'value':0,'step':0}]},
            'artifacts':[{'id':'file','path':'weights/best.pt','root':'/etc','artifact_uri':'file:///etc','size':0}]})
        self.post('/api/agent/jobs/observe',event,self.auth)
        record=self.service.get('run',run['id'])
        self.assertEqual(record['artifacts'],[])
        self.assertEqual(record['metrics']['accuracy'][0]['value'],0)

    def test_job_agent_duplicate_response_stop_and_replaced_uid(self):
        run = self.launch();command = self.poll()[0]
        token = self.home/'sa-token';token.write_text('sa')
        remote = {'job': None, 'pods': [], 'posts': 0, 'deletes': 0}
        def handler(req):
            path = req.url.path
            self.assertEqual(req.headers['Authorization'], 'Bearer sa')
            if path.endswith('/pods'):return httpx.Response(200, json={'items': remote['pods']})
            if '/nodes/' in path:return httpx.Response(200, json={'metadata': {'uid': NODE['uid']}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}})
            if '/persistentvolumeclaims/' in path:return httpx.Response(200, json={'metadata': {'uid': path.rsplit('/',1)[-1]+'-uid'}, 'status': {'phase': 'Bound'}})
            if req.method == 'POST':
                remote['posts'] += 1;remote['job'] = json.loads(req.content);remote['job']['metadata']['uid'] = 'job-uid'
                return httpx.Response(201, json=remote['job'])
            if req.method == 'DELETE':
                self.assertEqual(json.loads(req.content)['preconditions'], {'uid': 'job-uid'})
                remote['deletes'] += 1;remote['job'] = None;return httpx.Response(200, json={})
            return httpx.Response(200, json=remote['job']) if remote['job'] else httpx.Response(404)
        with httpx.Client(transport=httpx.MockTransport(handler), base_url='https://kube.test') as http:
            inventory = KubernetesInventory(http, token, 'training')
            executor = JobAgent(inventory, {'images': [IMAGE], 'pvcs': ['data','results'],'results_pvc':'results'},self.home)
            state = {**self.ident, 'agent_token': self.token, 'jobs': {}}
            path = self.home/'state.json'
            job_cycle(self.client, executor, state, path)
            # Simulated restart reloads the durable UID. Repeated polling reuses Job.
            state = json.loads(path.read_text());job_cycle(self.client, executor, state, path)
            self.assertEqual(remote['posts'], 1)
            remote['job']['metadata']['uid'] = 'wrong-uid'
            with self.assertRaisesRegex(ValueError, 'UID'):
                executor.reconcile(self.poll()[0])
            remote['job']['metadata']['uid'] = 'job-uid'
            self.post('/api/runs/'+run['id']+'/stop', {'name': run['name']})
            job_cycle(self.client, executor, state, path)
            self.assertNotEqual(self.service.get('run', run['id'])['state'], 'stopped')
            job_cycle(self.client, executor, state, path)
            self.assertEqual(self.service.get('run', run['id'])['state'], 'stopped')
            self.assertEqual(remote['deletes'], 1)
            self.assertEqual(remote['posts'], 1)

    def test_typed_evaluation_zero_metrics_and_lineage(self):
        run = self.launch()
        body = {'run_id': run['id'], 'dataset_id': self.dataset['id'], 'type': 'classification',
                'policy': {'evaluator_version': 'v1', 'split': 'validation', 'checkpoint_sha256': 'b'*64},
                'samples': [{'index': 0, 'target': '12', 'prediction': '21', 'confidence': 0.0, 'slice': 'blurred'}]}
        report = self.post('/api/analysis/reports', body)
        self.assertEqual(self.post('/api/analysis/reports', body)['id'], report['id'])
        data = self.client.get('/api/analysis/reports/'+report['id'], headers=OP).json()
        self.assertEqual(data['summary']['accuracy'], 0)
        self.assertEqual(data['confusion'][0]['count'], 1)
        self.assertEqual(data['items'][0]['confidence'], 0)
        self.assertEqual(self.client.get('/api/datasets/'+self.dataset['id']+'/lineage',headers=OP).json()['runs'][0]['id'], run['id'])
        body['samples'] *= 2
        self.assertEqual(self.client.post('/api/analysis/reports', json=body, headers=OP).status_code, 422)

    def test_evaluation_job_pins_checkpoint_and_preserves_model_identity(self):
        source=self.launch()
        with self.service.store.connect() as db:
            source.update(state='succeeded',artifacts=[{'id':'best','path':'completed/best.pt','complete':True,
                'role':'inference_weights','sha256':'a'*64,'artifact_uri':'mlflow-artifacts:/1/run/artifacts'}])
            self.service.store.put(db,'run',source)
        body={**self.request,'name':'independent evaluation','relation':'evaluate','parent_id':source['id'],
              'checkpoint_id':'best','evaluation':{'split':'validation','confidence':.01,'iou':.6}}
        preview=self.post('/api/preview',body)
        self.assertEqual(preview['frozen']['evaluation']['checkpoint_sha256'],'a'*64)
        self.assertNotIn('checkpoint_sha256',preview['frozen']['evaluation_policy'][0])
        result=self.post('/api/submissions',{**body,'confirmation_hash':preview['confirmation_hash']},{**OP,'Idempotency-Key':'evaluation-request'})
        self.assertEqual(result['pinocchio_id'],source['pinocchio_id'])
        self.assertEqual(self.poll()[0]['frozen']['relation'],'evaluate')
        self.assertEqual(self.client.post('/api/preview',json={**body,'checkpoint_id':'missing'},headers=OP).status_code,422)

    def test_sweep_atomicity_budget_concurrency_cancel_and_replay(self):
        request = {'name': 'batch sweep', 'training': self.request, 'method': 'random', 'seed': 42,
                   'parameters': {'batch': [4, 8, 16]}, 'max_trials': 2, 'concurrency': 1, 'metric': 'accuracy', 'direction': 'maximize'}
        preview = self.post('/api/sweeps/preview', request)
        self.assertEqual(preview, self.post('/api/sweeps/preview', request))
        self.assertEqual(self.service.list('run')['total'], 0)
        request.update(confirmation_hash=preview['confirmation_hash'], request_id='c'*32)
        sweep = self.post('/api/sweeps', request)
        self.assertEqual(self.post('/api/sweeps', request)['id'], sweep['id'])
        self.assertEqual(self.service.list('run')['total'], 2)
        self.assertEqual(len(self.poll()), 1)
        self.post('/api/sweeps/'+sweep['id']+'/cancel', {})
        commands = self.poll()
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]['action'], 'stop')
        pending = [self.service.get('run', t['run_id']) for t in sweep['trials']]
        self.assertTrue(any(r['state'] == 'stopped' for r in pending))


    def test_pipeline_dependencies_and_cycle_rejection(self):
        base=self.launch('pipeline-base')
        request={'name':'pipeline','nodes':[{'key':'a','base_run_id':base['id'],'after':[]},{'key':'b','base_run_id':base['id'],'after':['a']}]}
        reviewed=self.post('/api/pipelines/preview',request)
        created=self.post('/api/pipelines',{**request,'confirmation_hash':reviewed['confirmation_hash'],'request_id':'a'*32})
        from geppetto.pipelines import ready
        with self.service.store.connect() as db:
            runs=self.service.store.all(db,'run');first=next(r for r in runs if r.get('pipeline_step')=='a');second=next(r for r in runs if r.get('pipeline_step')=='b')
            self.assertTrue(ready(self.service.store,db,first,runs));self.assertFalse(ready(self.service.store,db,second,runs))
            first['state']='failed';self.service.store.put(db,'run',first)
            self.assertFalse(ready(self.service.store,db,second,runs));self.assertEqual(second['state'],'skipped')
        self.post('/api/pipelines/'+created['id']+'/cancel',{})
        self.assertIsNotNone(self.service.get('pipeline',created['id'])['cancelled_at'])
        pending=self.post('/api/pipelines',{**request,'confirmation_hash':reviewed['confirmation_hash'],'request_id':'b'*32})
        self.post('/api/pipelines/'+pending['id']+'/cancel',{})
        self.assertTrue(all(self.service.get('run',step['run_id'])['stop_requested_at'] for step in pending['steps']))
        request['nodes'][0]['after']=['b']
        self.assertEqual(self.client.post('/api/pipelines/preview',json=request,headers=OP).status_code,422)

    def test_managed_resume_pins_original_config_and_image(self):
        source=self.launch('resume-parent')
        with self.service.store.connect() as db:
            source.update(state='stopped',artifacts=[{'id':'resume','path':'completed/resume.pt','role':'resume_checkpoint','resume':True,'complete':True,'sha256':'f'*64,'artifact_uri':'mlflow-artifacts:/1/id/artifacts'}])
            self.service.store.put(db,'run',source)
        request={**self.request,'name':'resume','parent_id':source['id'],'relation':'resume','overrides':{'epochs':1}}
        review=self.post('/api/preview',request)
        self.assertEqual(review['frozen']['config'],source['frozen']['config'])
        self.assertEqual(review['frozen']['resume']['checkpoint_sha256'],'f'*64)
        self.post('/api/whales/'+self.node['id']+'/execution',{**self.profile_body,'image':'registry.test/other@sha256:'+'b'*64})
        changed=self.post('/api/preview',request)
        self.assertEqual(changed['frozen']['execution']['profile']['image'],IMAGE)


    def test_hyperband_has_multiple_reviewed_resource_brackets(self):
        body={'name':'Hyperband','training':self.request,'method':'hyperband','max_trials':6,'seed':3,
              'parameters':{'batch':[1,2,3,4,5,6]},'metric':'score','direction':'maximize',
              'pruning':{'min_step':1,'max_step':9,'reduction_factor':3}}
        review=self.post('/api/sweeps/preview',body)
        self.assertEqual(len(review['spec']['brackets']),3)
        self.assertEqual({t['bracket'] for t in review['spec']['trials']},{0,1,2})
        self.assertTrue(all(t['frozen']['config']['epochs']==10 for t in review['spec']['trials']))
        created=self.post('/api/sweeps',{**body,'confirmation_hash':review['confirmation_hash'],'request_id':'e'*32})
        self.assertEqual(len(created['trials']),6)
        body['parameters']['epochs']=[2,3]
        self.assertEqual(self.client.post('/api/sweeps/preview',json=body,headers=OP).status_code,422)


class ContentVersionTests(unittest.TestCase):
    def test_new_version_all_samples_and_source_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory);app=create_app(home/'app','catalog',False);s=app.state.service
            root=home/'source';(root/'images').mkdir(parents=True);(root/'labels').mkdir()
            splits={}
            for split in ('train','validation'):
                paths=[]
                for i in range(5):
                    image=root/'images'/f'{split}-{i}.jpg';image.write_bytes(f'{split}-{i}'.encode())
                    (root/'labels'/f'{split}-{i}.txt').write_text('0 0.5 0.5 0.2 0.2')
                    paths.append(str(image))
                path=home/(split+'.txt');path.write_text('\n'.join(paths));splits[split]=path
            s.roots=[str(root)]
            old=import_yolo_splits(s.store,home/'app',root,splits,'fixture')
            from geppetto.dataset_versions import request
            job=request(s,old['id']);self.assertEqual(request(s,old['id'])['id'],job['id'])
            tick(s,home/'app');job=s.get('dataset_verification',job['id'])
            self.assertEqual(job['state'],'succeeded')
            new=s.get('dataset',job['result_id'])
            self.assertFalse(s.get('dataset',old['id'])['content_verified'])
            self.assertTrue(new['content_verified']);verify_manifest(new)
            self.assertIn('image_file',sample_at(new,9))
            output=home/'export'/'datasets.json';export_registry(s,new['id'],output,'/datasets/ball')
            exported=json.loads(output.read_text())[new['id']]
            self.assertEqual(len(exported['samples']),10)
            self.assertEqual(exported['root'],'/datasets/ball')
            (root/'images/validation-4.jpg').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'changed'):verify_manifest(new)

    def test_duplicate_content_across_splits_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory);s=create_app(home/'app','catalog',False).state.service
            root=home/'data';root.mkdir();(root/'one').write_bytes(b'same');(root/'two').write_bytes(b'same')
            s.roots=[str(root)]
            d=s.register_dataset({'name':'duplicate','category':'ball-detection','root':str(root),
                'samples':[{'image':'one','split':'train'},{'image':'two','split':'validation'}]})
            from geppetto.dataset_versions import request
            job=request(s,d['id']);tick(s,home/'app')
            self.assertEqual(s.get('dataset_verification',job['id'])['state'],'failed')



if __name__ == '__main__':unittest.main()
