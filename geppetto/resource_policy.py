"""GPU admission, project quotas, weighted fairness and cooperative preemption."""
import time
from .service import TERMINAL


def gpu_count(run):
    gpu = run.get('frozen', {}).get('gpu', {})
    return gpu.get('count', 1) * len(gpu.get('node_ids') or [run.get('node_id')])


def held(run):
    return bool(run.get('dispatch')) and (run['state'] not in TERMINAL or not run.get('resource_released'))


def configure(service, project_id, body):
    if service.mode == 'readonly':
        raise ValueError('Read-only mode')
    service.get('project', project_id)
    value = {'id': project_id, 'updated_at': time.time()}
    for key, default, low, high in [('max_gpus', 64, 0, 4096), ('guaranteed_gpus', 0, 0, 4096),
                                   ('weight', 1, 1, 100), ('max_running', 64, 1, 1000)]:
        n = body.get(key, default)
        if type(n) is not int or not low <= n <= high:
            raise ValueError('Invalid project resource policy: ' + key)
        value[key] = n
    if value['guaranteed_gpus'] > value['max_gpus']:
        raise ValueError('Guaranteed GPUs cannot exceed the project quota')
    value['allow_preemption'] = body.get('allow_preemption') is True
    with service.store.connect() as db:
        service.store.put(db, 'resource_policy', value)
    return value


def project(run):
    return run.get('project_id') or run.get('frozen', {}).get('project_id', '')


def rank(run, runs, policies):
    pid = project(run)
    p = policies.get(pid, {})
    used = sum(gpu_count(r) for r in runs if held(r) and project(r) == pid)
    # An unmet guaranteed share precedes borrowed capacity; weights divide current consumption.
    return (used >= p.get('guaranteed_gpus', 0), -run.get('priority', 0),
            used / p.get('weight', 1), run['created_at'], run['id'])


def admission(run, runs, policies, node):
    p = policies.get(project(run), {})
    active = [r for r in runs if held(r)]
    own = [r for r in active if project(r) == project(run)]
    if sum(gpu_count(r) for r in own) + gpu_count(run) > p.get('max_gpus', 4096):
        return 'Waiting for project GPU quota'
    if len(own) >= p.get('max_running', 1000):
        return 'Waiting for project concurrency limit'
    capacity = int(node.get('allocatable', {}).get('nvidia.com/gpu', 0))
    used = sum(r['frozen']['gpu']['count'] for r in active if run['node_id'] in (r['frozen']['gpu'].get('node_ids') or [r['node_id']]))
    allocation = node.get('allocation')
    if allocation and time.time()-allocation.get('observed_at', 0) <= 60:
        known = sum(allocation.get('run_gpus', {}).get(r['id'], 0) for r in active) if 'run_gpus' in allocation else allocation.get('managed_gpu', 0)
        used += max(0, allocation.get('gpu', 0)-known)
    if used + run['frozen']['gpu']['count'] > capacity:
        return 'Waiting for all requested GPUs'
    return None


def preempt(service, db, waiting, runs, policies):
    """Only opt-in managed jobs with an already published resume checkpoint can yield."""
    requester = policies.get(project(waiting), {})
    if not requester.get('allow_preemption'):
        return False
    candidates = sorted((r for r in runs if held(r) and r['node_id'] == waiting['node_id']
                         and project(r) != project(waiting) and r.get('priority', 0) < waiting.get('priority', 0)
                         and r.get('frozen', {}).get('preemptible') and not r.get('stop_requested_at')),
                        key=lambda r: (r.get('priority', 0), -r['created_at']))
    for victim in candidates:
        checkpoint = next((a for a in victim.get('artifacts', []) if a.get('resume') and a.get('complete')), None)
        if not checkpoint:
            continue
        vp = policies.get(project(victim), {})
        used = sum(gpu_count(r) for r in runs if held(r) and project(r) == project(victim))
        if used - gpu_count(victim) < vp.get('guaranteed_gpus', 0):
            continue
        victim.update(stop_requested_at=time.time(), stop_reason='Resources yielded to a higher-priority job', preempted_by=waiting['id'])
        service.store.put(db, 'run', victim)
        return True
    return False
