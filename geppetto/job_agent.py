"""Reconcile deterministic Jobs inside the Agent's explicitly allowed namespace."""
import json
import time
from pathlib import Path
from urllib.parse import quote
import httpx
from .configuration import digest
from .execution import job_manifest
from .files import open_beneath


class JobAgent:
    def __init__(self, inventory, policy, results_root=None):
        self.kube = inventory
        self.policy = policy
        self.results_root = Path(results_root) if results_root else None

    def call(self, method, path, **kwargs):
        response = self.kube.client.request(method, path, headers={
            'Authorization': 'Bearer ' + self.kube.token_file.read_text().strip()}, **kwargs)
        response.raise_for_status()
        return response

    def optional(self, path, params=None):
        try:
            return self.call('GET', path, params=params).json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    def owned(self, job, command):
        meta = job['metadata']
        if (meta.get('labels', {}).get('geppetto.run-id') != command['id'] or
                meta.get('labels', {}).get('app.kubernetes.io/managed-by') != 'geppetto-agent' or
                meta.get('annotations', {}).get('geppetto.spec-sha256') != digest(command['frozen'])):
            raise ValueError('Existing Job has a different owner or specification')
        previous = command['dispatch'].get('job_uid')
        if previous and previous != meta['uid']:
            raise ValueError('Job UID differs from the acknowledged Job')

    def reconcile(self, command):
        if command['frozen']['execution'].get('members') and not command.get('result_id'):
            return self.reconcile_group(command)
        p = command['frozen']['execution']['profile']
        if p['namespace'] != self.kube.namespace:
            raise ValueError('Job namespace is outside Agent scope')
        if command['action']!='stop' and p['image'] not in self.policy.get('images', []):
            raise ValueError('Image digest is not in Agent allowlist')
        if command['action']!='stop' and any(c['name'] not in self.policy.get('pvcs', []) for c in p['claims'].values()):
            raise ValueError('PVC is not in Agent allowlist')
        if command['action']!='stop' and (not self.results_root or not self.results_root.is_dir() or self.policy.get('results_pvc')!=p['claims']['results']['name']):
            raise ValueError('Mount this profile results PVC read-only in the Agent before dispatch')
        ns = '/api/v1/namespaces/' + quote(p['namespace'], safe='')
        base = '/apis/batch/v1/namespaces/' + quote(p['namespace'], safe='') + '/jobs'
        path = base + '/' + command['dispatch']['name']
        job = self.optional(path)
        selector = {'labelSelector': 'geppetto.run-id=' + command['id'] + ',app.kubernetes.io/managed-by=geppetto-agent'}
        pods = self.call('GET', ns + '/pods', params=selector).json().get('items', [])
        if job:
            self.owned(job, command)
            uid = job['metadata']['uid']
        else:
            uid = command['dispatch'].get('job_uid')
        for pod in pods:
            owners = pod['metadata'].get('ownerReferences', [])
            if not uid or not any(o.get('uid') == uid and o.get('kind') == 'Job' for o in owners):
                raise ValueError('Pod owner does not match the managed Job')
        active = [p for p in pods if p.get('status', {}).get('phase') not in ('Succeeded', 'Failed')]
        if command['action'] == 'stop':
            if job and not job['metadata'].get('deletionTimestamp'):
                self.call('DELETE', path, json={'apiVersion': 'v1', 'kind': 'DeleteOptions',
                    'propagationPolicy': 'Foreground', 'preconditions': {'uid': uid}})
            released = not job and not active
            return {'state': 'stopped' if released else 'submitting', 'job_uid': uid,
                    'phase': 'Stopped' if released else 'Waiting for job termination and resource release', 'resource_released': released,
                    'result': self.results(command)}
        if not job:
            if uid:
                return {'state': 'unknown', 'job_uid': uid, 'error': 'Acknowledged Job is missing; automatic recreation is disabled'}
            node = self.call('GET', '/api/v1/nodes/' + quote(p['node_name'], safe='')).json()
            if node['metadata']['uid'] != p['node_uid']:
                raise ValueError('Node UID changed; select the replacement node explicitly')
            if node.get('spec', {}).get('unschedulable') or not any(c['type'] == 'Ready' and c['status'] == 'True' for c in node.get('status', {}).get('conditions', [])):
                return {'state': 'queued', 'phase': 'Waiting for selected node readiness'}
            for claim in p['claims'].values():
                observed = self.call('GET', ns + '/persistentvolumeclaims/' + claim['name']).json()
                if observed['metadata']['uid'] != claim['uid'] or observed.get('status', {}).get('phase') != 'Bound':
                    raise ValueError('PVC changed or is not bound')
            try:
                job = self.call('POST', base, json=job_manifest(command)).json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 409:
                    raise
                job = self.call('GET', path).json()
            self.owned(job, command)
            uid = job['metadata']['uid']
        conditions = job.get('status', {}).get('conditions', [])
        state = ('succeeded' if any(c['type'] == 'Complete' and c['status'] == 'True' for c in conditions)
                 else 'failed' if any(c['type'] == 'Failed' and c['status'] == 'True' for c in conditions)
                 else 'running' if any(p.get('status', {}).get('phase') == 'Running' for p in pods) else 'queued')
        reason = next((c.get('message') for pod in pods for c in pod.get('status', {}).get('conditions', [])
                       if c.get('type') == 'PodScheduled' and c.get('status') == 'False'), None)
        observation = {'state': state, 'job_uid': uid, 'phase': reason or state, 'resource_released': not active and state in ('succeeded', 'failed')}
        if state == 'failed':
            reasons = [p.get('status', {}).get('reason') for p in pods]
            observation['failure_kind'] = 'infrastructure' if any(r in ('Evicted', 'NodeLost', 'Shutdown') for r in reasons) else 'training'
        from datetime import datetime
        starts=[];finishes=[]
        for pod in pods:
            for container in pod.get('status',{}).get('containerStatuses',[]):
                state_info=container.get('state',{})
                timing=state_info.get('running') or state_info.get('terminated') or {}
                if timing.get('startedAt'):starts.append(datetime.fromisoformat(timing['startedAt'].replace('Z','+00:00')).timestamp())
                if timing.get('finishedAt'):finishes.append(datetime.fromisoformat(timing['finishedAt'].replace('Z','+00:00')).timestamp())
        if starts:observation['started_at']=min(starts)
        if finishes and state in ('succeeded','failed'):observation['finished_at']=max(finishes)
        if pods:
            pod = max(pods, key=lambda p: p['metadata'].get('creationTimestamp', ''))
            if pod.get('status', {}).get('phase') != 'Pending':
                try:
                    observation['logs'] = self.call('GET', ns + '/pods/' + pod['metadata']['name'] + '/log',
                                                    params={'container': 'train', 'tailLines': 1000, 'limitBytes': 128000}).text
                except httpx.HTTPStatusError:
                    pass  # Log retention is independent of the observed execution state.
        result = self.results(command)
        if result:
            observation['result'] = result
        if state == 'succeeded' and not command.get('result_id') and (not result or result.get('state') != 'succeeded'):
            observation.update(state='failed' if result and result.get('state') in ('failed','stopped') else 'unknown',
                               phase='Job completed · Result state mismatch or awaiting publication confirmation')
        return observation

    def reconcile_group(self, command):
        from .distributed import commands, service_manifest
        members = commands(command)
        cleanup = command['dispatch'].get('group_failure')
        if cleanup:
            for member in members: member['action'] = 'stop'
        observed = []
        for index, member in enumerate(members):
            observation = self.reconcile(member)
            observed.append(observation)
            if index == 0 and observation.get('job_uid') and command['action'] != 'stop' and not cleanup:
                manifest = service_manifest(command, observation['job_uid'])
                base = '/api/v1/namespaces/'+quote(self.kube.namespace, safe='')+'/services'
                existing = self.optional(base+'/'+manifest['metadata']['name'])
                if not existing:
                    try: self.call('POST', base, json=manifest)
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code != 409: raise
                        existing = self.call('GET', base+'/'+manifest['metadata']['name']).json()
                if existing and (existing['metadata'].get('labels', {}).get('geppetto.group-id') != command['id'] or
                        not any(o.get('uid') == observation['job_uid'] for o in existing['metadata'].get('ownerReferences', []))):
                    raise ValueError('Rendezvous service ownership differs')
        ids = {str(i): o['job_uid'] for i, o in enumerate(observed) if o.get('job_uid')}
        states = [o['state'] for o in observed]
        failing = bool(cleanup)
        failure_kind = command['dispatch'].get('group_failure_kind') or ('infrastructure' if 'queued' in states or any(o.get('failure_kind') == 'infrastructure' for o in observed) else 'training')
        if cleanup:
            state = 'failed' if all(o.get('resource_released') for o in observed) else 'running'
        elif command['action'] == 'stop':
            state = 'stopped' if all(o.get('resource_released') for o in observed) else 'submitting'
        elif 'failed' in states or (time.time()-command['dispatch']['created_at'] > 600 and 'queued' in states):
            failing = True
            # Release every member on failure or rendezvous starvation; no orphan GPU reservations.
            released = []
            for member, observation in zip(members, observed):
                member['action'] = 'stop'
                if observation.get('job_uid'): member['dispatch']['job_uid'] = observation['job_uid']
                released.append(self.reconcile(member).get('resource_released', False))
            state = 'failed' if all(released) else 'running'
            for o, done in zip(observed, released): o['resource_released'] = done
        elif all(s == 'succeeded' for s in states): state = 'succeeded'
        elif 'running' in states: state = 'running'
        else: state = 'queued'
        result = self.results(command)
        if state == 'succeeded' and (not result or result.get('state') != 'succeeded'):
            state = 'unknown'
        return {'state': state, 'job_uid': ids.get('0') or command['dispatch'].get('job_uid'), 'member_uids': ids, 'group_failure': failing, 'group_failure_kind': failure_kind,
                'phase': 'Distributed job '+state, 'resource_released': all(o.get('resource_released') for o in observed),
                'failure_kind': failure_kind,
                'logs': '\n'.join('[node '+str(i)+']\n'+o.get('logs', '') for i, o in enumerate(observed)),
                'result': result,
                **({'started_at': min(o['started_at'] for o in observed if 'started_at' in o)} if any('started_at' in o for o in observed) else {})}

    def results(self, command):
        if not self.results_root:
            return None
        # Per-profile root mapping prevents reading another volume under the same run ID.
        p = command['frozen']['execution']['profile']
        if self.policy.get('results_pvc') != p['claims']['results']['name']:
            return None
        result_id = command.get('result_id', command['id'])
        result = {'submission_id': result_id}
        for name in ('tracking.json', 'progress.json', 'checkpoint.json', 'result.json'):
            try:
                with open_beneath(self.results_root, result_id + '/' + name) as stream:
                    raw = stream.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise ValueError('Result metadata exceeds 8 MiB')
                data = json.loads(raw)
            except FileNotFoundError:
                continue
            if name == 'tracking.json':
                result['mlflow'] = [{'run_id': data['mlflow_run_id'], 'experiment_id': data['experiment_id']}]
                result['artifact_uri'] = data.get('artifact_uri')
            else:
                if data.get('submission_id') != result_id:
                    raise ValueError('Result belongs to another execution')
                result.update({k: data[k] for k in ('metrics', 'state', 'model_version', 'live_checkpoint') if k in data})
                if name == 'result.json':
                    result['artifacts'] = [{**a, 'artifact_uri': result.get('artifact_uri', '')} for a in data.get('artifacts', [])]
        return result


def cycle(central, executor, state, path):
    from .agent import save
    auth = {'X-Geppetto-Request': '1', 'Authorization': 'Bearer ' + state['agent_token']}
    identity = {k: state[k] for k in ('cluster_id', 'cluster_uid', 'agent_id')}
    response = central.post('/api/agent/jobs/poll', json=identity, headers=auth)
    response.raise_for_status()
    for command in response.json()['commands']:
        # Persist a successful create's UID before contacting central. Lost replies never duplicate Jobs.
        ledger = state.setdefault('jobs', {}).setdefault(command['id'], {'sequence': 0})
        if ledger.get('job_uid'):
            command['dispatch']['job_uid'] = ledger['job_uid']
        if ledger.get('group_failure'):
            command['dispatch']['group_failure'] = True
            command['dispatch']['group_failure_kind'] = ledger.get('group_failure_kind', 'training')
        if ledger.get('member_uids'):
            command['dispatch']['member_uids'] = ledger['member_uids']
        ledger['sequence'] = max(ledger['sequence'], command['dispatch'].get('sequence', 0)) + 1
        save(path, state)
        try:
            observation = executor.reconcile(command)
        except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
            observation = {'state': 'unknown', 'error': type(exc).__name__ + ': ' + str(exc)[:800]}
        if observation.get('job_uid'):
            ledger['job_uid'] = observation['job_uid']
            if observation.get('member_uids'): ledger['member_uids'] = observation['member_uids']
            if observation.get('group_failure'):
                ledger['group_failure'] = True
                ledger['group_failure_kind'] = observation.get('group_failure_kind', 'training')
            save(path, state)
        response = central.post('/api/agent/jobs/observe', headers=auth,
            json={**identity, 'run_id': command['id'], 'sequence': ledger['sequence'], **observation})
        response.raise_for_status()
