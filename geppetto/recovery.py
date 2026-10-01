"""Durable retry attempts: immutable old runs, distinct output paths and Job identities."""
import copy
import time
from .configuration import digest
from .service import TERMINAL


def policy(value=None):
    value = value or {}
    result = {'max_retries': value.get('max_retries', 0), 'delay_seconds': value.get('delay_seconds', 60),
              'backoff': value.get('backoff', 2), 'on': value.get('on', 'infrastructure')}
    for key, low, high in [('max_retries', 0, 10), ('delay_seconds', 0, 86400), ('backoff', 1, 10)]:
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('Invalid retry policy: ' + key)
    if result['on'] not in ('infrastructure', 'any_failure'):
        raise ValueError('Unknown retry condition')
    return result


def clone(service, db, old, key, now):
    if old.get('retry_run_id'):
        return service.store.get(db, 'run', old['retry_run_id'])
    item = {k: copy.deepcopy(old[k]) for k in ('name', 'category', 'template_id', 'frozen', 'synthetic',
            'node_id', 'pinocchio_id', 'model_id', 'project_id', 'priority', 'pipeline_id', 'pipeline_step',
            'sweep_id', 'sweep_bracket', 'schedule_id', 'logical_date') if k in old}
    item.update(id=digest(['retry', old['id'], key])[:32], created_at=now, retry_of=old['id'],
                retry_root=old.get('retry_root', old['id']))
    child = service.new_run(item)
    child['attempt'] = old.get('attempt', 0) + 1
    child['frozen'].pop('resolved_input', None)
    old['retry_run_id'] = child['id']
    service.store.put(db, 'run', old)
    service.store.put(db, 'run', child)
    if old.get('pipeline_id'):
        pipeline = service.store.get(db, 'pipeline', old['pipeline_id'])
        step = next(s for s in pipeline['steps'] if s['key'] == old['pipeline_step'])
        step.setdefault('attempts', []).append(old['id'])
        step['run_id'] = child['id']
        service.store.put(db, 'pipeline', pipeline)
    if old.get('sweep_id'):
        sweep = service.store.get(db, 'sweep', old['sweep_id'])
        for trial in sweep['trials']:
            if trial['run_id'] == old['id']:
                trial.setdefault('attempts', []).append(old['id'])
                trial['run_id'] = child['id']
        service.store.put(db, 'sweep', sweep)
    return child


def eligible(run):
    return (run.get('state') == 'failed' and run.get('resource_released') is True
            and not run.get('stop_requested_at') and not run.get('imported')
            and run.get('frozen', {}).get('execution', {}).get('backend') == 'agent')


def retry(service, id, body):
    if service.mode == 'readonly':
        raise ValueError('Read-only mode')
    with service.store.connect() as db:
        old = service.store.get(db, 'run', id)
        if old.get('retry_run_id'):
            return service.store.get(db, 'run', old['retry_run_id'])
        if not eligible(old):
            raise ValueError('Retry a failed managed run after its resources have been released')
        if body.get('name') != old['name']:
            raise ValueError('Confirm the execution name')
        if old.get('pipeline_id'):
            p = service.store.get(db, 'pipeline', old['pipeline_id'])
            if p.get('cancelled_at'):
                raise ValueError('Cancelled pipelines cannot be restarted')
        child = clone(service, db, old, 'manual', time.time())
        if old.get('pipeline_id'):
            # Reactivate only skipped descendants; successful ancestors remain untouched.
            p = service.store.get(db, 'pipeline', old['pipeline_id'])
            affected = {old['pipeline_step']}
            while True:
                expanded = affected | {s['key'] for s in p['steps'] if affected.intersection(s['after'])}
                if expanded == affected:
                    break
                affected = expanded
            for step in p['steps']:
                if step['key'] in affected and step['key'] != old['pipeline_step']:
                    r = service.store.get(db, 'run', step['run_id'])
                    if r['state'] == 'skipped':
                        clone(service, db, r, 'upstream-retry', time.time())
        return child


def tick(service, db, now=None):
    now = time.time() if now is None else now
    for run in service.store.all(db, 'run'):
        if run.get('preempted_by') and run['state'] == 'stopped' and run.get('resource_released') and not run.get('retry_run_id'):
            if run.get('pipeline_id') and service.store.get(db, 'pipeline', run['pipeline_id']).get('cancelled_at'):
                continue
            if run.get('sweep_id') and service.store.get(db, 'sweep', run['sweep_id'])['state'] != 'active':
                continue
            checkpoint = next((a for a in run.get('artifacts', []) if a.get('resume') and a.get('complete') and a.get('sha256')), None)
            if checkpoint:
                child = clone(service, db, run, 'preemption-resume', now)
                from .execution import relative
                child['frozen'].update(relation='resume', parent_id=run['id'], resume={
                    'parent_id': run['id'], 'checkpoint_uri': checkpoint['artifact_uri'].rstrip('/')+'/'+relative(checkpoint['path']),
                    'checkpoint_sha256': checkpoint['sha256']})
                child['attempt'] = run.get('attempt', 0)
                child['phase'] = 'Waiting to resume from checkpoint after preemption'
                service.store.put(db, 'run', child)
            continue
        if not eligible(run) or run.get('retry_run_id'):
            continue
        p = policy(run.get('frozen', {}).get('retry'))
        if run.get('attempt', 0) >= p['max_retries']:
            continue
        if p['on'] == 'infrastructure' and run.get('failure_kind') != 'infrastructure':
            continue
        if run.get('pipeline_id') and service.store.get(db, 'pipeline', run['pipeline_id']).get('cancelled_at'):
            continue
        if run.get('sweep_id') and service.store.get(db, 'sweep', run['sweep_id'])['state'] != 'active':
            continue
        due = run.get('finished_at', now) + min(86400, p['delay_seconds'] * p['backoff'] ** run.get('attempt', 0))
        run['retry_at'] = due
        service.store.put(db, 'run', run)
        if due <= now:
            clone(service, db, run, 'automatic', now)
