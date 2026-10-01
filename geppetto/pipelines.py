"""Reviewed training/evaluation/registry DAGs with artifact binding and durable attempts."""
import copy
import re
import time
from .configuration import digest
from .service import TERMINAL
from .recovery import policy


def preview(service, body):
    nodes = body.get('nodes', [])
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 50:
        raise ValueError('Provide 1–50 pipeline steps')
    ids = [n.get('key') for n in nodes]
    if any(not isinstance(k, str) or not re.fullmatch('[a-z][a-z0-9_-]{0,39}', k) for k in ids) or len(set(ids)) != len(ids):
        raise ValueError('Unique step keys required')
    steps, graph = [], {}
    for node in nodes:
        dependencies = node.get('after', [])
        if not isinstance(dependencies, list) or len(set(dependencies)) != len(dependencies) or any(k not in ids or k == node['key'] for k in dependencies):
            raise ValueError('Invalid dependencies')
        graph[node['key']] = dependencies
        kind = node.get('type', 'train')
        if kind not in ('train', 'evaluate', 'register'):
            raise ValueError('Step type must be train, evaluate or register')
        source = node.get('input_from')
        if source and source not in dependencies or kind in ('evaluate', 'register') and not source:
            raise ValueError('Select a direct predecessor as the input source')
        if node.get('training'):
            request = copy.deepcopy(node['training'])
            request['name'] = str(body.get('name', 'Pipeline'))[:100] + ' · ' + node['key']
            request['relation'] = 'train'
            request.pop('parent_id', None)
            if node.get('dataset_ids'): request['dataset_ids'] = node['dataset_ids']
            if node.get('overrides'): request['overrides'] = node['overrides']
            if node.get('gpu'): request['gpu'] = node['gpu']
            request['retry'] = policy(node.get('retry'))
            request['priority'] = node.get('priority', 0)
        else:
            base = service.get('run', node['base_run_id'])
            frozen = base['frozen']
            request = {'name': str(body.get('name', 'Pipeline'))[:100] + ' · ' + node['key'], 'node_id': base['node_id'],
                       'project_id': base.get('project_id') or frozen['project_id'],
                       'template_id': base['template_id'], 'dataset_ids': node.get('dataset_ids', [d['id'] for d in frozen['datasets']]),
                       'overrides': node.get('overrides', frozen['config']), 'gpu': node.get('gpu', frozen['gpu']),
                       'retry': policy(node.get('retry')), 'priority': node.get('priority', 0), 'relation': 'train'}
        if kind in ('evaluate', 'register'):
            request['gpu'] = {'mode': 'auto', 'count': 1}
        reviewed = service.preview(request)['frozen']
        if reviewed.get('execution', {}).get('backend') != 'agent':
            raise ValueError('Pipelines require Kubernetes Agent execution')
        if kind == 'evaluate' or source and kind == 'train':
            if reviewed['template']['adapter'] != 'ball':
                raise ValueError('Artifact inputs currently require the ball adapter')
        evaluation = node.get('evaluation', {'split': 'test', 'confidence': .001, 'iou': .7})
        if kind == 'evaluate':
            from .analytics import finite
            split = evaluation.get('split', 'test')
            if split not in ('test', 'validation') or any(not service.get('dataset', d)['splits'].get(split) for d in request['dataset_ids']):
                raise ValueError('Select a nonempty evaluation split')
            confidence, iou = finite(evaluation.get('confidence', .001)), finite(evaluation.get('iou', .7))
            if not 0 <= confidence <= 1 or not 0 < iou <= 1:
                raise ValueError('Invalid evaluation thresholds')
            evaluation = {'split': split, 'confidence': confidence, 'iou': iou, 'max_det': 300,
                          'precision': 'fp32', 'evaluator_version': reviewed['template']['code_version']}
            reviewed['gpu'] = {'mode': 'auto', 'count': 1}
        alias = node.get('alias', 'candidate')
        if kind == 'register' and (not re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,63}', alias) or re.fullmatch('v[0-9]+', alias)):
            raise ValueError('Invalid model alias')
        gate = node.get('gate')
        if gate:
            from .analytics import finite
            if gate.get('step') not in dependencies or gate.get('operator') not in ('>=', '<=') or not isinstance(gate.get('metric'), str):
                raise ValueError('Quality gate requires a predecessor metric, operator and threshold')
            gate = {**gate, 'threshold': finite(gate['threshold'])}
        reviewed['pipeline_action'] = kind
        steps.append({'key': node['key'], 'type': kind, 'after': dependencies, 'input_from': source,
                      'checkpoint': node.get('checkpoint', 'best.pt'), 'evaluation': evaluation,
                      'alias': alias, 'gate': gate, 'request': request, 'frozen': reviewed})
    done = set()
    while len(done) < len(ids):
        ready = {k for k in ids if k not in done and all(d in done for d in graph[k])}
        if not ready:
            raise ValueError('Pipeline contains a dependency cycle')
        done.update(ready)
    by_key = {s['key']: s for s in steps}
    for step in steps:
        if step['input_from']:
            source = by_key[step['input_from']]
            if source['type'] != 'train':
                raise ValueError('Model inputs must refer to a training step')
            if source['frozen']['template']['id'] != step['frozen']['template']['id']:
                raise ValueError('Connected steps must use the same model template')
    spec = {'name': str(body.get('name', 'Pipeline'))[:160], 'steps': steps}
    return {'spec': spec, 'confirmation_hash': digest(spec)}


def create(service, body):
    reviewed = preview(service, body)
    if reviewed['confirmation_hash'] != body.get('confirmation_hash'):
        raise ValueError('Review pipeline steps before starting')
    key = body.get('request_id', '')
    if not re.fullmatch('[a-f0-9]{32}', key):
        raise ValueError('UUID request ID required')
    item = {'id': key, **reviewed['spec'], 'digest': reviewed['confirmation_hash'], 'created_at': time.time()}
    with service.store.connect() as db:
        old = next((p for p in service.store.all(db, 'pipeline') if p['id'] == key), None)
        if old:
            if old['digest'] != item['digest']:
                raise ValueError('Pipeline request ID conflict')
            return old
        from .workshop import new_pinocchio
        for step in item['steps']:
            frozen = step['frozen']
            pin = new_pinocchio(service.store, db, frozen['node_id'], frozen['template'], step['request']['name'])
            pin['project_id'] = frozen['project_id']
            service.store.put(db, 'pinocchio', pin)
            run = service.new_run({'id': digest([key, step['key']])[:32], 'name': step['request']['name'], 'created_at': time.time(),
                'template_id': frozen['template']['id'], 'category': frozen['template']['category'], 'frozen': copy.deepcopy(frozen),
                'synthetic': False, 'node_id': frozen['node_id'], 'pinocchio_id': pin['id'], 'model_id': pin['id'],
                'project_id': pin['project_id'], 'pipeline_id': key, 'pipeline_step': step['key'], 'priority': frozen.get('priority', 0)})
            step['run_id'] = run['id']
            service.store.put(db, 'run', run)
        service.store.put(db, 'pipeline', item)
    return item


def ready(store, db, run, runs):
    if not run.get('pipeline_id'):
        return True
    pipeline = store.get(db, 'pipeline', run['pipeline_id'])
    step = next(s for s in pipeline['steps'] if s['key'] == run['pipeline_step'])
    ids = {s['key']: s['run_id'] for s in pipeline['steps']}
    parents = [store.get(db, 'run', ids[k]) for k in step['after']]
    # A failed parent may still be in its automatic retry delay.
    if any(p.get('retry_at') and not p.get('retry_run_id') for p in parents):
        return False
    if any(p['state'] in TERMINAL and p['state'] != 'succeeded' for p in parents):
        run.update(state='skipped', phase='Predecessor failed or stopped', finished_at=time.time(), resource_released=True)
        store.put(db, 'run', run)
        return False
    if not all(p['state'] == 'succeeded' for p in parents):
        return False
    if run['frozen'].get('resolved_input'):
        return True
    try:
        gate = step.get('gate')
        if gate:
            values = store.get(db, 'run', ids[gate['step']]).get('metrics', {}).get(gate['metric'], [])
            if not values:
                raise ValueError('Quality gate metric was not recorded')
            value = values[-1]['value']
            if not (value >= gate['threshold'] if gate['operator'] == '>=' else value <= gate['threshold']):
                raise ValueError('Evaluation quality gate failed')
        if step.get('input_from'):
            source = store.get(db, 'run', ids[step['input_from']])
            frozen = run['frozen']
            if step.get('type') == 'register':
                if not source.get('model_version'):
                    raise ValueError('No registered model version from the preceding training run')
                frozen['registry_source'] = source['id']
            else:
                artifact = next((a for a in source['artifacts'] if a.get('complete') and a.get('role') == 'inference_weights'
                                 and a['path'].split('/')[-1] == step.get('checkpoint', 'best.pt')), None)
                if not artifact or not artifact.get('sha256') or not artifact.get('artifact_uri', '').startswith('mlflow-artifacts:/'):
                    raise ValueError('No verified checkpoint from the preceding training run')
                from .execution import relative
                weights = {'checkpoint_uri': artifact['artifact_uri'].rstrip('/') + '/' + relative(artifact['path']),
                           'checkpoint_sha256': artifact['sha256']}
                frozen['parent_id'] = source['id']
                if step.get('type') == 'evaluate':
                    frozen.update(relation='evaluate', evaluation={**step['evaluation'], **weights}, evaluation_policy=[step['evaluation']])
                else:
                    frozen['input_weights'] = weights
            frozen['resolved_input'] = {'run_id': source['id'], 'at': time.time()}
        store.put(db, 'run', run)
        return True
    except ValueError as exc:
        run.update(state='failed', phase=str(exc), failure_kind='validation', finished_at=time.time(), resource_released=True)
        store.put(db, 'run', run)
        return False


def tick_registry(service):
    if service.mode == 'readonly':
        return
    for row in service.list('run', limit=100000)['items']:
        if row.get('frozen', {}).get('pipeline_action') != 'register' or row['state'] in TERMINAL:
            continue
        with service.store.connect() as db:
            run = service.store.get(db, 'run', row['id'])
            if run.get('stop_requested_at'):
                run.update(state='stopped', finished_at=time.time(), resource_released=True)
                service.store.put(db, 'run', run)
                continue
            if not ready(service.store, db, run, []):
                continue
            pipeline = service.store.get(db, 'pipeline', run['pipeline_id'])
            step = next(s for s in pipeline['steps'] if s['key'] == run['pipeline_step'])
        from .registry import preview as registry_preview, promote
        try:
            body = {'run_id': run['frozen']['registry_source'], 'alias': step['alias']}
            review = registry_preview(service, body)
            result = promote(service, {**body, 'confirmation_hash': review['confirmation_hash']})
            if result['state'] != 'confirmed':
                raise ValueError('Could not verify the model alias update')
            run.update(state='succeeded', phase='Model alias assigned', registry_audit=result['id'])
        except Exception as exc:
            run.update(state='failed', phase=str(exc)[:1000], failure_kind='registry')
        run.update(finished_at=time.time(), resource_released=True)
        with service.store.connect() as db:
            service.store.put(db, 'run', run)


def cancel(service, id):
    if service.mode == 'readonly':
        raise ValueError('Read-only mode')
    with service.store.connect() as db:
        item = service.store.get(db, 'pipeline', id)
        item['cancelled_at'] = time.time()
        for step in item['steps']:
            run = service.store.get(db, 'run', step['run_id'])
            if run['state'] not in TERMINAL:
                run.update(stop_requested_at=item['cancelled_at'], stop_reason='pipeline cancelled')
                service.store.put(db, 'run', run)
        service.store.put(db, 'pipeline', item)
    return item
