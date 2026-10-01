import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import test_platform as fixtures
from test_platform import OP, NODE
from geppetto.configuration import digest
from geppetto.execution import job_manifest
from geppetto.resource_policy import admission, rank
from geppetto.node_metrics import clean, quantity, HostCollector
from geppetto.distributed import commands, service_manifest


class OrchestrationTests(unittest.TestCase):
    setUp = fixtures.PlatformTests.setUp
    tearDown = fixtures.PlatformTests.tearDown
    post = fixtures.PlatformTests.post
    launch = fixtures.PlatformTests.launch
    poll = fixtures.PlatformTests.poll
    observation = fixtures.PlatformTests.observation

    def finish(self, run, state='succeeded', **extra):
        result = {'submission_id': run['id'], 'state': state, **extra.pop('result', {})}
        return self.post('/api/agent/jobs/observe', self.observation(run, state=state, resource_released=True,
                         result=result, **extra), self.auth)

    def graph(self, steps):
        body = {'name': 'linked flow', 'nodes': steps}
        review = self.post('/api/pipelines/preview', body)
        return self.post('/api/pipelines', {**body, 'confirmation_hash': review['confirmation_hash'], 'request_id': 'c'*32})

    def test_checkpoint_flow_failure_retry_and_successful_ancestor_preserved(self):
        base = self.launch()
        graph = self.graph([
            {'key': 'train', 'base_run_id': base['id'], 'after': []},
            {'key': 'eval', 'type': 'evaluate', 'base_run_id': base['id'], 'after': ['train'], 'input_from': 'train',
             'evaluation': {'split': 'validation'}},
            {'key': 'later', 'base_run_id': base['id'], 'after': ['eval']}])
        training = self.service.get('run', graph['steps'][0]['run_id'])
        self.poll()
        artifact = {'id': 'best', 'path': 'completed/best.pt', 'sha256': 'a'*64, 'size': 4,
                    'artifact_uri': 'mlflow-artifacts:/r/artifacts', 'role': 'inference_weights'}
        self.finish(training, result={'artifacts': [artifact]})
        commands = self.poll()
        evaluation = self.service.get('run', graph['steps'][1]['run_id'])
        selected = next(c for c in commands if c['id'] == evaluation['id'])
        self.assertEqual(selected['frozen']['relation'], 'evaluate')
        self.assertEqual(selected['frozen']['evaluation']['checkpoint_sha256'], 'a'*64)
        self.assertTrue(selected['frozen']['evaluation']['checkpoint_uri'].endswith('/completed/best.pt'))
        self.finish(evaluation, 'failed')
        self.poll()
        downstream = self.service.get('run', graph['steps'][2]['run_id'])
        self.assertEqual(downstream['state'], 'skipped')
        retried = self.post('/api/runs/'+evaluation['id']+'/retry', {'name': evaluation['name']})
        self.assertNotEqual(retried['id'], evaluation['id'])
        again = self.post('/api/runs/'+evaluation['id']+'/retry', {'name': evaluation['name']})
        self.assertEqual(again['id'], retried['id'])
        current = self.service.get('pipeline', graph['id'])
        self.assertEqual(current['steps'][0]['run_id'], training['id'])
        self.assertEqual(self.service.get('run', training['id'])['state'], 'succeeded')
        self.assertNotEqual(current['steps'][2]['run_id'], downstream['id'])
        self.assertEqual(self.service.get('run', current['steps'][2]['run_id'])['state'], 'queued')

    def test_bounded_automatic_retry_waits_for_release_and_backoff(self):
        self.request['retry'] = {'max_retries': 1, 'delay_seconds': 30}
        run = self.launch(); self.poll()
        self.finish(run, 'failed', failure_kind='infrastructure', finished_at=100)
        from geppetto.recovery import tick
        with self.service.store.connect() as db:
            tick(self.service, db, 120)
            self.assertNotIn('retry_run_id', self.service.store.get(db, 'run', run['id']))
            tick(self.service, db, 131)
            old = self.service.store.get(db, 'run', run['id'])
            child = self.service.store.get(db, 'run', old['retry_run_id'])
            self.assertEqual(child['attempt'], 1)
            child.update(state='failed', finished_at=150, resource_released=True, failure_kind='infrastructure')
            self.service.store.put(db, 'run', child)
            tick(self.service, db, 10000)
            self.assertNotIn('retry_run_id', self.service.store.get(db, 'run', child['id']))

    def test_training_failure_not_automatically_retried_by_default(self):
        self.request['retry'] = {'max_retries': 3, 'delay_seconds': 0}
        run = self.launch(); self.poll(); self.finish(run, 'failed', failure_kind='training')
        self.poll()
        self.assertNotIn('retry_run_id', self.service.get('run', run['id']))

    def test_backfill_preview_timezone_replay_and_changed_confirmation(self):
        self.request['schedule'] = {'kind': 'cron', 'cron': '0 9 * * *', 'timezone': 'Asia/Seoul'}
        schedule = self.launch()
        body = {'start': '2026-01-01T00:00:00+00:00', 'end': '2026-01-03T00:00:00+00:00'}
        url = '/api/schedules/'+schedule['id']+'/backfill'
        preview = self.post(url+'/preview', body)
        self.assertEqual(len(preview['spec']['occurrences']), 3)
        result = self.post(url, {**body, 'confirmation_hash': preview['confirmation_hash']})
        self.assertEqual(len(result['run_ids']), 3)
        self.assertEqual(self.post(url, {**body, 'confirmation_hash': preview['confirmation_hash']}), result)
        after = self.post(url+'/preview', body)
        self.assertTrue(all(d['exists'] for d in after['spec']['occurrences']))
        self.assertTrue(all(self.service.get('run', id)['logical_date'] in [d['at'] for d in preview['spec']['occurrences']] for id in result['run_ids']))
        self.assertEqual(self.service.get('schedule', schedule['id'])['next_at'], schedule['next_at'])

    def test_scheduler_does_not_overwrite_completed_backfill_occurrence(self):
        self.request['schedule'] = {'kind':'cron','cron':'0 9 * * *','timezone':'Asia/Seoul'}
        schedule = self.launch()
        body = {'start':'2026-01-01T00:00:00+00:00','end':'2026-01-01T00:00:00+00:00'}
        url = '/api/schedules/'+schedule['id']+'/backfill'
        reviewed = self.post(url+'/preview', body)
        result = self.post(url, {**body,'confirmation_hash':reviewed['confirmation_hash']})
        with self.service.store.connect() as db:
            run = self.service.store.get(db,'run',result['run_ids'][0]);run['state']='succeeded'
            run['metrics']={'proof':[{'value':42}]};self.service.store.put(db,'run',run)
            schedule['next_at']=run['logical_date'];self.service.store.put(db,'schedule',schedule)
            self.service.tick_schedules(db,run['logical_date']+1)
            self.assertEqual(self.service.store.get(db,'run',run['id'])['metrics'],run['metrics'])
            self.assertEqual(self.service.store.get(db,'schedule',schedule['id'])['history'][-1]['result'],'already_exists')

    def test_new_pipeline_does_not_require_prior_training(self):
        reviewed = self.post('/api/pipelines/preview', {'name':'first pipeline','nodes':[
            {'key':'train','after':[],'training':self.request},
            {'key':'evaluate','type':'evaluate','after':['train'],'input_from':'train',
             'training':self.request,'evaluation':{'split':'validation'}}]})
        self.assertEqual([s['type'] for s in reviewed['spec']['steps']],['train','evaluate'])
        self.assertFalse(self.service.list('run')['items'])

    def test_project_quota_holds_failed_job_until_resource_release(self):
        self.post('/api/projects/project-ball-detection/resources', {'max_gpus': 1})
        first = self.launch('first'); second = self.launch('second')
        self.assertEqual([c['id'] for c in self.poll()], [first['id']])
        self.assertIn('project GPU quota', self.service.get('run', second['id'])['phase'])
        self.finish(first, 'failed')
        self.assertEqual([c['id'] for c in self.poll()], [second['id']])

    def test_multi_gpu_requires_profile_and_keeps_atomic_pod_request(self):
        self.request['gpu']['count'] = 2
        self.assertEqual(self.client.post('/api/preview', json=self.request, headers=OP).status_code, 422)
        self.post('/api/whales/'+self.node['id']+'/execution', {**self.profile_body, 'max_gpus': 2})
        self.request['overrides'] = {'batch': 4}
        run = self.launch(); command = self.poll()[0]
        self.assertEqual(command['id'], run['id'])
        self.assertEqual(job_manifest(command)['spec']['template']['spec']['containers'][0]['resources']['requests']['nvidia.com/gpu'], '2')
        self.request['overrides']['batch'] = 3
        self.assertEqual(self.client.post('/api/preview', json=self.request, headers=OP).status_code, 422)

    def test_multinode_reservation_profiles_and_group_identity(self):
        second = {**NODE, 'uid': 'two', 'name': 'two'}
        self.post('/api/agent/snapshot', {**self.ident, 'sequence': 2, 'nodes': [NODE, second],
            'pvcs': [{'uid': n+'-uid', 'name': n, 'namespace': 'training', 'phase': 'Bound', 'access_modes': ['ReadWriteMany']} for n in ('data', 'results')]}, self.auth)
        node = self.post('/api/clusters/'+self.cluster['id']+'/adopt', {'node_uid': 'two'})
        for id in (self.node['id'], node['id']):
            self.post('/api/whales/'+id+'/execution', {**self.profile_body, 'max_gpus': 2, 'shared_storage': True})
        self.post('/api/datasets/'+self.dataset['id']+'/locations', {'node_id': node['id'], 'namespace': 'training', 'pvc': 'data', 'root': 'ball'})
        self.request['gpu'] = {'mode': 'auto', 'count': 2, 'node_ids': [self.node['id'], node['id']]}
        self.request['overrides'] = {'batch': 4}
        run = self.launch(); command = self.poll()[0]
        group = commands(command)
        self.assertEqual(len(group), 2)
        self.assertNotEqual(group[0]['dispatch']['name'], group[1]['dispatch']['name'])
        for rank, member in enumerate(group):
            manifest = job_manifest(member)
            env = {v['name']: v['value'] for v in manifest['spec']['template']['spec']['containers'][0]['env']}
            self.assertEqual(env['GEPPETTO_NODE_RANK'], str(rank))
            self.assertEqual(json.loads(env['GEPPETTO_SPEC_JSON'])['submission_id'], run['id'])
        service = service_manifest(command, 'uid')
        self.assertEqual(service['metadata']['ownerReferences'][0]['uid'], 'uid')

    def test_template_entrypoint_is_frozen_validated_and_backward_compatible(self):
        self.launch()
        command = self.poll()[0]
        container = job_manifest(command)['spec']['template']['spec']['containers'][0]
        self.assertEqual(container['command'], [
            'python', '/opt/geppetto/integrations/container_entrypoint.py'])
        custom = copy.deepcopy(command)
        custom['frozen']['template']['entrypoint'] = ['python', '/app/train.py']
        self.assertEqual(
            job_manifest(custom)['spec']['template']['spec']['containers'][0]['command'],
            ['python', '/app/train.py'])
        custom['frozen']['template'].pop('entrypoint')
        self.assertEqual(job_manifest(custom)['spec']['template']['spec']['containers'][0]['command'],
                         ['python', '/opt/geppetto/integrations/container_entrypoint.py'])
        custom['frozen']['template']['entrypoint'] = 'python /app/train.py'
        with self.assertRaises(ValueError): job_manifest(custom)

    def test_telemetry_survives_snapshot_and_staleness_is_explicit(self):
        metric = {'observed_at': time.time(), 'cpu_percent': 42, 'memory_used_bytes': 123, 'source': 'test',
                  'gpus': [{'uuid': 'GPU-one', 'name': 'A100', 'utilization_percent': 0}]}
        self.post('/api/agent/snapshot', {**self.ident, 'sequence': 2, 'nodes': [{**NODE, 'telemetry': metric}], 'pvcs': []}, self.auth)
        result = self.client.get('/api/whales/'+self.node['id']+'/resources', headers=OP).json()
        self.assertFalse(result['stale'])
        self.assertEqual(result['telemetry']['gpus'][0]['utilization_percent'], 0)
        self.assertIsNone(result['telemetry']['gpus'][0]['memory_used_mib'])
        self.assertEqual(len(result['history']), 1)
        with patch('geppetto.node_metrics.time.time', return_value=time.time()+120):
            result = self.client.get('/api/whales/'+self.node['id']+'/resources', headers=OP).json()
        self.assertTrue(result['stale'])

    def test_preempted_checkpoint_resumes_after_release(self):
        self.request['preemptible'] = True
        run = self.launch(); self.poll()
        with self.service.store.connect() as db:
            run = self.service.store.get(db, 'run', run['id'])
            run.update(state='stopped', preempted_by='urgent', resource_released=True,
                artifacts=[{'id': 'resume', 'path': 'completed/resume.pt', 'artifact_uri': 'mlflow-artifacts:/run',
                            'complete': True, 'resume': True, 'sha256': 'a'*64}])
            self.service.store.put(db, 'run', run)
            from geppetto.recovery import tick
            tick(self.service, db)
            old = self.service.store.get(db, 'run', run['id'])
            child = self.service.store.get(db, 'run', old['retry_run_id'])
            self.assertEqual(child['frozen']['relation'], 'resume')
            self.assertEqual(child['frozen']['resume']['checkpoint_sha256'], 'a'*64)
            self.assertEqual(child['attempt'], run['attempt'])


class MetricTests(unittest.TestCase):
    def test_units_and_invalid_numbers(self):
        self.assertEqual(quantity('500m'), .5)
        self.assertEqual(quantity('2Gi'), 2*1024**3)
        self.assertEqual(quantity('100000000n'), .1)
        with self.assertRaises(ValueError): clean({'cpu_percent': float('nan')})
        self.assertIsNone(clean({})['cpu_percent'])

    def test_fairness_uses_guarantees_and_weight(self):
        def run(id, project, dispatch=False):
            return {'id': id, 'project_id': project, 'created_at': 1, 'state': 'running' if dispatch else 'queued',
                    'dispatch': {'name': id} if dispatch else None, 'frozen': {'gpu': {'count': 1}}}
        a, b = run('a', 'one'), run('b', 'two')
        busy = run('busy', 'one', True)
        policies = {'one': {'guaranteed_gpus': 1}, 'two': {'guaranteed_gpus': 1}}
        self.assertLess(rank(b, [a, b, busy], policies), rank(a, [a, b, busy], policies))


class DistributedLifecycleTests(unittest.TestCase):
    def test_group_failure_cleans_every_member_and_survives_restart(self):
        import httpx
        from geppetto.job_agent import JobAgent
        from geppetto.agent import KubernetesInventory
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/'token').write_text('fixture')
            profile = {'namespace': 'training', 'image': 'image@sha256:'+'a'*64, 'node_name': 'one', 'node_uid': 'one-uid',
                       'claims': {'datasets': {'name': 'data', 'uid': 'data-uid'}, 'results': {'name': 'results', 'uid': 'results-uid'}},
                       'registry_path': 'datasets.json', 'mlflow_uri': 'http://mlflow'}
            two = {**profile, 'node_name': 'two', 'node_uid': 'two-uid'}
            command = {'id': 'run', 'action': 'reconcile', 'dispatch': {'name': 'geppetto-run', 'created_at': time.time()},
                       'frozen': {'gpu': {'count': 1}, 'execution': {'profile': profile, 'members': [profile, two]}}}
            jobs, services = {}, {}
            def handler(req):
                path = req.url.path
                if path.endswith('/pods'): return httpx.Response(200, json={'items': []})
                if '/nodes/' in path:
                    name = path.split('/')[-1]
                    return httpx.Response(200, json={'metadata': {'uid': name+'-uid'}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}})
                if '/persistentvolumeclaims/' in path:
                    return httpx.Response(200, json={'metadata': {'uid': path.split('/')[-1]+'-uid'}, 'status': {'phase': 'Bound'}})
                collection = services if '/services' in path else jobs
                if req.method == 'POST':
                    body = json.loads(req.content); body['metadata']['uid'] = body['metadata']['name']+'-uid'
                    collection[path+'/'+body['metadata']['name']] = body
                    return httpx.Response(200, json=body)
                if req.method == 'DELETE':
                    expected = json.loads(req.content)['preconditions']['uid']
                    self.assertEqual(collection[path]['metadata']['uid'], expected)
                    del collection[path]; return httpx.Response(200, json={})
                return httpx.Response(200, json=collection[path]) if path in collection else httpx.Response(404)
            with httpx.Client(base_url='https://kube.test', transport=httpx.MockTransport(handler)) as client:
                inventory = KubernetesInventory(client, root/'token', 'training')
                policy = {'images': [profile['image']], 'pvcs': ['data', 'results'], 'results_pvc': 'results'}
                executor = JobAgent(inventory, policy, root)
                observed = executor.reconcile(command)
                self.assertEqual(len(jobs), 2); self.assertEqual(len(services), 1)
                command['dispatch'].update(job_uid=observed['job_uid'], member_uids=observed['member_uids'])
                next(iter(jobs.values()))['status'] = {'conditions': [{'type': 'Failed', 'status': 'True'}]}
                observed = executor.reconcile(command)
                self.assertTrue(observed['group_failure']); self.assertFalse(jobs)
                command['dispatch']['group_failure'] = True
                command['dispatch']['group_failure_kind'] = 'infrastructure'
                restarted = JobAgent(inventory, policy, root)
                final = restarted.reconcile(command)
                self.assertEqual(final['state'], 'failed')
                self.assertEqual(final['failure_kind'], 'infrastructure')
                self.assertTrue(final['resource_released'])
                self.assertFalse(jobs)

    def test_torchrun_launcher_keeps_rank_callbacks_and_recovers_progress(self):
        from integrations.model_runner import Recorder, ball
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            mlflow = Mock(); mlflow.active_run.return_value.info.run_id = 'ml-run'
            recorder = Recorder(root, mlflow, 'submission')
            (root/'progress.json').write_text(json.dumps({'metrics': {'loss': [{'value': .5}]}}))
            child = Mock(); child.wait.return_value = 0
            with patch.dict('os.environ', {'GEPPETTO_GPU_COUNT': '2', 'GEPPETTO_NNODES': '1'}, clear=True), patch('integrations.model_runner.subprocess.Popen', return_value=child) as launch:
                ball({'batch': 4}, root, root, recorder, {'samples': []})
            args = launch.call_args.args[0]
            self.assertIn('--nproc-per-node=2', args)
            self.assertTrue(any(str(arg).endswith('distributed_ball.py') for arg in args))
            self.assertEqual(recorder.metrics['loss'][0]['value'], .5)
