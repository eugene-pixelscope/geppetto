"""Durable, cluster-scoped independent Jobs. Airflow never owns these runs."""
import copy
import json
import re
import time
from pathlib import PurePosixPath
from fastapi import HTTPException
from .clusters import authorize, public
from .configuration import digest
from .service import TERMINAL


def dns(value):
    if not isinstance(value, str) or len(value) > 63 or not re.fullmatch(r'[a-z0-9]([-a-z0-9]*[a-z0-9])?', value):
        raise ValueError('Invalid Kubernetes resource name')
    return value


def relative(value):
    if not isinstance(value, str) or not value or value.startswith('/') or '\\' in value or any(p in ('', '.', '..') for p in value.split('/')):
        raise ValueError('Expected a path relative to the registered volume')
    return value


def configure(service, node_id, body):
    if service.mode == 'readonly':
        raise ValueError('Read-only mode')
    node = service.get('whale', node_id)
    cluster = service.get('cluster', node['cluster_id'])
    template = service.get('template', body['template_id'])
    image = body.get('image', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}', image):
        raise ValueError('Use an immutable image reference with sha256 digest')
    namespace = dns(body['namespace'])
    claims = {}
    for role in ('datasets', 'results'):
        name = dns(body[role + '_pvc'])
        claim = next((p for p in cluster['pvcs'] if p['namespace'] == namespace and p['name'] == name and p['phase'] == 'Bound'), None)
        if not claim:
            raise ValueError('A bound, discovered PVC is required: ' + name)
        claims[role] = {'name': name, 'uid': claim['uid']}
    if claims['datasets']['name'] == claims['results']['name']:
        raise ValueError('Data and writable results must use different PVCs')
    concurrency = body.get('concurrency', 1)
    if type(concurrency) is not int or not 1 <= concurrency <= 64:
        raise ValueError('Node concurrency must be 1–64')
    mlflow = body.get('mlflow_uri', '')
    from urllib.parse import urlparse
    parsed = urlparse(mlflow)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Provide the MLflow server URI without embedded credentials')
    max_gpus = body.get('max_gpus', 1)
    if type(max_gpus) is not int or not 1 <= max_gpus <= 64:
        raise ValueError('Maximum GPUs per job must be 1–64')
    cpu_cores = body.get('cpu_cores', 4)
    memory_gib = body.get('memory_gib', 16)
    shm_gib = body.get('shm_gib', 8)
    if type(cpu_cores) is not int or not 1 <= cpu_cores <= 256:
        raise ValueError('CPU cores per job must be 1–256')
    if type(memory_gib) is not int or not 1 <= memory_gib <= 2048:
        raise ValueError('Memory per job must be 1–2048 GiB')
    if type(shm_gib) is not int or not 1 <= shm_gib <= memory_gib:
        raise ValueError('Shared memory must be 1 GiB through the job memory limit')
    shared_storage = body.get('shared_storage') is True
    if shared_storage:
        result_claim = next(p for p in cluster['pvcs'] if p['uid'] == claims['results']['uid'])
        if 'ReadWriteMany' not in result_claim.get('access_modes', []):
            raise ValueError('Shared training requires a discovered ReadWriteMany results PVC')
    profile = {'shared_storage': shared_storage, 'max_gpus': max_gpus, 'cpu_cores': cpu_cores,
               'memory_gib': memory_gib, 'shm_gib': shm_gib,
               'id': digest([node_id, template['id']])[:32], 'node_id': node_id,
               'cluster_id': node['cluster_id'], 'node_uid': node['node_uid'], 'node_name': node['host'],
               'namespace': namespace, 'template_id': template['id'], 'image': image, 'claims': claims,
               'registry_path': relative(body.get('registry_path', 'geppetto/datasets.json')),
               'mlflow_uri': mlflow.rstrip('/'), 'concurrency': concurrency,
               'enabled': body.get('enabled') is True, 'updated_at': time.time()}
    with service.store.connect() as db:
        service.store.put(db, 'execution_profile', profile)
        node = service.store.get(db, 'whale', node_id)
        node['template_ids'] = sorted(set(node['template_ids'] + [template['id']]))
        node['description'] = 'Kubernetes Agent · Independent training jobs'
        service.store.put(db, 'whale', node)
    return profile


def bind_dataset(service, dataset_id, body):
    """A binding is a location assertion; the runner still verifies every byte."""
    if service.mode == 'readonly':
        raise ValueError('Read-only mode')
    node = service.get('whale', body['node_id'])
    if node.get('connection') != 'kubernetes-agent':
        raise ValueError('Select a discovered Agent node')
    cluster = service.get('cluster', node['cluster_id'])
    namespace = dns(body['namespace'])
    pvc = dns(body['pvc'])
    claim = next((p for p in cluster['pvcs'] if p['namespace'] == namespace and p['name'] == pvc and p['phase'] == 'Bound'), None)
    if not claim:
        raise ValueError('Select a bound, discovered data PVC')
    with service.store.connect() as db:
        item = service.store.get(db, 'dataset', dataset_id)
        locations = item.setdefault('locations', [])
        location = {'node_id': node['id'], 'cluster_id': cluster['id'], 'namespace': namespace,
                    'pvc': pvc, 'pvc_uid': claim['uid'], 'version': item['version'],
                    'root': relative(body['root']), 'registered_at': time.time()}
        item['locations'] = [l for l in locations if l['node_id'] != node['id']] + [location]
        service.store.put(db, 'dataset', item)
    return location


def available(dataset, node_id):
    return dataset.get('node_id') == node_id or any(l['node_id'] == node_id and l['version'] == dataset['version'] for l in dataset.get('locations', []))


def freeze(service, node, template, datasets):
    profiles = service.list('execution_profile', limit=100000)['items']
    profile = next((p for p in profiles if p['node_id'] == node['id'] and p['template_id'] == template['id'] and p['enabled']), None)
    if not profile:
        raise ValueError('Register and enable a pinned execution profile for this node/model')
    cluster = service.get('cluster', node['cluster_id'])
    if public(cluster)['state'] != 'connected' or not node.get('present') or node['node']['ready'] != 'True' or node['node']['unschedulable']:
        raise ValueError('The selected node must be connected, Ready and schedulable')
    locations = []
    for dataset in datasets:
        if dataset.get('status') == 'indexed' or dataset.get('content_verified') is False or dataset.get('synthetic'):
            raise ValueError('Full dataset content verification is required')
        location = next((l for l in dataset.get('locations', []) if l['node_id'] == node['id'] and l['version'] == dataset['version']), None)
        if not location or location['pvc_uid'] != profile['claims']['datasets']['uid'] or location['namespace'] != profile['namespace']:
            raise ValueError('Bind this dataset version to the execution profile data PVC')
        locations.append(copy.deepcopy(location))
    return {'backend': 'agent', 'profile': copy.deepcopy(profile), 'locations': locations}


def authenticated(store, db, body, credential):
    cluster = store.get(db, 'cluster', body.get('cluster_id', ''))
    authorize(cluster, credential)
    if not cluster.get('approved'):
        raise HTTPException(403, 'Agent approval required')
    if body.get('cluster_uid') != cluster['cluster_uid'] or body.get('agent_id') != cluster['agent_id']:
        raise HTTPException(409, 'Agent identity mismatch')
    return cluster


def poll(service, body, credential):
    with service.store.connect() as db:
        cluster = authenticated(service.store, db, body, credential)
        if service.mode == 'readonly':
            return {'commands': []}
        service.tick_schedules(db, time.time())
        from .recovery import tick as retry_tick
        retry_tick(service, db)
        runs = service.store.all(db, 'run')
        from .sweeps import control
        control(service.store,db,runs)
        from .resource_policy import held, rank, admission, preempt
        policies = {p['id']: p for p in service.store.all(db, 'resource_policy')}
        counts = {}
        for run in runs:
            if held(run):
                for node_id in run['frozen']['gpu'].get('node_ids') or [run['node_id']]:
                    counts[node_id] = counts.get(node_id, 0) + 1
        commands = []
        pending = list(runs)
        while pending:
            run = min(pending, key=lambda r: rank(r, runs, policies))
            pending.remove(run)
            execution = run.get('frozen', {}).get('execution', {})
            if execution.get('backend') != 'agent' or run.get('imported') or (run['state'] in TERMINAL and (not run.get('dispatch') or run.get('resource_released'))) or run['frozen'].get('pipeline_action') == 'register':
                continue
            profile = execution['profile']
            if profile['cluster_id'] != cluster['id']:
                continue
            if run.get('stop_requested_at') and not run.get('dispatch'):
                run.update(state='stopped', resource_released=True, finished_at=time.time())
                service.store.put(db, 'run', run)
                continue
            if not run.get('dispatch'):
                from .pipelines import ready
                if not ready(service.store,db,run,runs):
                    continue
                from .sweeps import can_dispatch
                if not can_dispatch(service.store,db,run,runs):
                    continue
                if public(cluster)['state'] != 'connected':
                    continue
                current = service.store.get(db, 'execution_profile', profile['id'])
                node = next((n for n in cluster['nodes'] if n['uid'] == profile['node_uid']), None)
                if not current['enabled'] or not node or node['ready'] != 'True' or node['unschedulable']:
                    run['phase'] = 'Waiting for node or execution settings'
                    service.store.put(db, 'run', run)
                    continue
                reason = admission(run, runs, policies, node)
                for member in execution.get('members', [])[1:]:
                    member_node = next((n for n in cluster['nodes'] if n['uid'] == member['node_uid']), None)
                    member_profile = service.store.get(db, 'execution_profile', member['id'])
                    if not member_node or member_node['ready'] != 'True' or member_node['unschedulable'] or not member_profile['enabled']:
                        reason = 'Waiting for distributed training nodes'
                        break
                    import copy
                    member_run = copy.deepcopy(run);member_run['node_id'] = member['node_id']
                    reason = reason or admission(member_run, runs, policies, member_node)
                    if counts.get(member['node_id'], 0) >= member_profile['concurrency']:
                        reason = 'Waiting for distributed node concurrency limits'
                if counts.get(run['node_id'], 0) >= current['concurrency']:
                    reason = 'Waiting for node concurrency limit'
                if reason:
                    run['phase'] = reason
                    service.store.put(db, 'run', run)
                    if reason in ('Waiting for all requested GPUs', 'Waiting for node concurrency limit'):
                        preempt(service, db, run, runs, policies)
                    continue
                run['dispatch'] = {'name': 'geppetto-' + run['id'], 'created_at': time.time(), 'sequence': -1}
                run.update(state='submitting', phase='Awaiting agent submission')
                for node_id in run['frozen']['gpu'].get('node_ids') or [run['node_id']]:
                    counts[node_id] = counts.get(node_id, 0) + 1
                service.store.put(db, 'run', run)
            commands.append({'id': run['id'], 'action': 'stop' if run.get('stop_requested_at') else 'reconcile',
                             'dispatch': run['dispatch'], 'frozen': run['frozen']})
            if len(commands) >= 100:
                break
    return {'commands': commands}


def observe(service, body, credential):
    with service.store.connect() as db:
        cluster = authenticated(service.store, db, body, credential)
        run = service.store.get(db, 'run', body['run_id'])
        profile = run.get('frozen', {}).get('execution', {}).get('profile', {})
        if profile.get('cluster_id') != cluster['id'] or not run.get('dispatch'):
            raise HTTPException(403, 'Run is not owned by this Agent')
        sequence = body.get('sequence')
        if type(sequence) is not int or sequence < 0:
            raise ValueError('Invalid observation sequence')
        dispatch = run['dispatch']
        if sequence <= dispatch['sequence']:
            if sequence == dispatch['sequence'] and digest(body) == dispatch.get('digest'):
                return {'accepted': True, 'duplicate': True}
            raise HTTPException(409, 'Stale observation')
        uid = body.get('job_uid')
        if dispatch.get('job_uid') and uid and uid != dispatch['job_uid']:
            raise HTTPException(409, 'Job UID changed')
        state = body.get('state')
        if state not in ('submitting', 'queued', 'running', 'succeeded', 'failed', 'stopped', 'unknown'):
            raise ValueError('Invalid Job state')
        if state == 'stopped' and (not run.get('stop_requested_at') or body.get('resource_released') is not True):
            raise ValueError('Stop requires observed resource release')
        if state=='succeeded' and not body.get('error'):
            result=body.get('result') or {}
            if result.get('state')!='succeeded' or result.get('submission_id')!=run['id']:
                raise ValueError('Success requires a finalized result from this submission')
        if run['state'] in TERMINAL and state != run['state']:
            raise HTTPException(409, 'Terminal state is immutable')
        error = body.get('error')
        run.update(observed_at=time.time(), fetch_error=str(error)[:1000] if error else None)
        if not error:
            run.update(state=state, phase=str(body.get('phase', state))[:1000], last_success_at=time.time())
            if body.get('started_at') is not None:
                from .analytics import finite
                run.setdefault('started_at',finite(body['started_at']))
            if state in TERMINAL:
                from .analytics import finite
                run.setdefault('finished_at',finite(body.get('finished_at',time.time())))
            if body.get('logs') is not None:
                run['logs'] = str(body['logs'])[-128000:].splitlines()[-2000:]
            if body.get('failure_kind') in ('infrastructure', 'training', 'validation'):
                run['failure_kind'] = body['failure_kind']
            if body.get('resource_released'):
                run['resource_released'] = True
            result = body.get('result')
            if result:
                # Never accept remote filesystem roots as central file paths.
                if result.get('submission_id') != run['id']:
                    raise ValueError('Result submission differs')
                if result.get('metrics') is not None:
                    from .analytics import validate_histories
                    run['metrics'] = validate_histories(result['metrics'])
                if result.get('mlflow'):
                    run['mlflow'] = result['mlflow']
                if result.get('live_checkpoint') and state not in TERMINAL:
                    a = result['live_checkpoint']
                    if a.get('artifact_uri', '').startswith('mlflow-artifacts:/') and re.fullmatch('[a-f0-9]{64}', a.get('sha256','')):
                        run['artifacts'] = [{'id':'live-resume','path':relative(a['path']),'sha256':a['sha256'],
                            'size':a.get('size',0),'role':'resume_checkpoint','resume':True,'complete':True,
                            'remote':True,'artifact_uri':a['artifact_uri']}]
                if result.get('model_version'):
                    run['model_version'] = result['model_version']
                if result.get('artifacts') and state in TERMINAL:
                    artifacts = []
                    for a in result['artifacts']:
                        path = relative(a['path'])
                        if not a.get('artifact_uri', '').startswith('mlflow-artifacts:/'):
                            continue
                        artifacts.append({k: a[k] for k in ('id', 'size', 'sha256', 'role', 'artifact_uri') if k in a} |
                                         {'path': path, 'complete': True, 'remote': True, 'resume': a.get('resume') is True and a.get('role')=='resume_checkpoint'})
                    run['artifacts'] = artifacts
        dispatch.update(sequence=sequence, digest=digest(body))
        if body.get('group_failure'):
            dispatch['group_failure'] = True
            dispatch['group_failure_kind'] = body.get('group_failure_kind', 'training')
        if body.get('member_uids'):
            members = body['member_uids']
            if any(k in dispatch.get('member_uids', {}) and dispatch['member_uids'][k] != v for k, v in members.items()):
                raise HTTPException(409, 'Distributed member UID changed')
            dispatch.setdefault('member_uids', {}).update(members)
        if uid:
            dispatch['job_uid'] = uid
        service.store.put(db, 'run', run)
    return {'accepted': True}



LEGACY_ENTRYPOINT = ['python', '/opt/geppetto/integrations/container_entrypoint.py']


def template_entrypoint(template):
    """Return an exec-form container command; old frozen runs keep their legacy path."""
    command = template.get('entrypoint', LEGACY_ENTRYPOINT)
    if (not isinstance(command, list) or not 1 <= len(command) <= 32
            or any(not isinstance(arg, str) or not arg or len(arg) > 4096 or '\0' in arg
                   for arg in command)):
        raise ValueError('Template entrypoint must be 1–32 nonempty command arguments')
    return list(command)

def job_manifest(command):
    frozen = command['frozen']
    p = frozen['execution']['profile']
    labels = {'app.kubernetes.io/managed-by': 'geppetto-agent', 'geppetto.run-id': command['id']}
    result_id = command.get('result_id', command['id'])
    spec = {'submission_id': result_id, 'frozen': frozen}
    if len(json.dumps(spec).encode()) > 200000:
        raise ValueError('Execution specification exceeds 200 KB')
    resources = {'cpu': str(p.get('cpu_cores', 4)), 'memory': str(p.get('memory_gib', 16))+'Gi',
                 'nvidia.com/gpu': str(frozen['gpu']['count'])}
    manifest = {'apiVersion': 'batch/v1', 'kind': 'Job',
            'metadata': {'name': command['dispatch']['name'], 'namespace': p['namespace'], 'labels': labels,
                         'annotations': {'geppetto.spec-sha256': digest(frozen)}},
            'spec': {'backoffLimit': 0, 'parallelism': 1, 'completions': 1,
                     'template': {'metadata': {'labels': labels}, 'spec': {
                         'restartPolicy': 'Never', 'terminationGracePeriodSeconds': 120, 'automountServiceAccountToken': False,
                         'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001, 'fsGroup': 10001},
                         'affinity': {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {
                             'nodeSelectorTerms': [{'matchFields': [{'key': 'metadata.name', 'operator': 'In', 'values': [p['node_name']]}]}]}}},
                         'containers': [{'name': 'train', 'image': p['image'], 'imagePullPolicy': 'IfNotPresent',
                             'command': template_entrypoint(frozen.get('template', {})),
                             'env': [{'name':'OMP_NUM_THREADS','value':'4'},{'name':'MKL_NUM_THREADS','value':'4'},{'name':'GIT_PYTHON_REFRESH','value':'quiet'}, {'name': 'GEPPETTO_SPEC_JSON', 'value': json.dumps(spec)},
                                     {'name': 'GEPPETTO_DATASET_REGISTRY', 'value': '/datasets/' + p['registry_path']},
                                     {'name': 'GEPPETTO_OUTPUT', 'value': '/outputs/' + result_id},
                                     {'name': 'MLFLOW_TRACKING_URI', 'value': p['mlflow_uri']}],
                             'resources': {'requests': resources, 'limits': resources},
                             'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                             'volumeMounts': [{'name': 'datasets', 'mountPath': '/datasets', 'readOnly': True},
                                              {'name': 'results', 'mountPath': '/outputs'}, {'name': 'cache', 'mountPath': '/cache'},
                                              {'name': 'shm', 'mountPath': '/dev/shm'}]}],
                         'volumes': [{'name': role, 'persistentVolumeClaim': {'claimName': claim['name'], 'readOnly': role=='datasets'}}
                                     for role, claim in p['claims'].items()] +
                                    [{'name': 'cache', 'emptyDir': {}}, {'name': 'shm', 'emptyDir': {'medium': 'Memory',
                                                                                          'sizeLimit': str(p.get('shm_gib', 8))+'Gi'}}]}}}}

    group = frozen['execution'].get('group')
    if group:
        container = manifest['spec']['template']['spec']['containers'][0]
        container['env'] += [{'name': 'GEPPETTO_NODE_RANK', 'value': str(group['rank'])},
                            {'name': 'GEPPETTO_NNODES', 'value': str(group['size'])},
                            {'name': 'GEPPETTO_MASTER', 'value': group['master']+':29400'},
                            {'name': 'GEPPETTO_GROUP', 'value': group['id']}]
    return manifest
