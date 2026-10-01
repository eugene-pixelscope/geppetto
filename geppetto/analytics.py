"""Explicit metric semantics and reproducible, typed evaluation reports."""
import math
import time
import uuid
from collections import Counter, defaultdict
from .configuration import digest


def finite(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('A finite numeric value is required')
    return value


def validate_histories(metrics):
    if not isinstance(metrics, dict) or len(metrics) > 200:
        raise ValueError('At most 200 metrics are supported')
    for name, points in metrics.items():
        if not isinstance(name, str) or not 1 <= len(name) <= 160 or not isinstance(points, list) or len(points) > 100000:
            raise ValueError('Invalid metric history')
        for p in points:
            finite(p['value'])
            for axis in ('step', 'epoch', 'elapsed', 'timestamp'):
                if p.get(axis) is not None:
                    finite(p[axis])
    return metrics


def define_metric(service, body):
    if service.mode == 'readonly':
        raise ValueError('Read-only mode')
    project = service.get('project', body['project_id'])
    key = body['key']
    if not isinstance(key, str) or not 1 <= len(key) <= 160:
        raise ValueError('Invalid metric key')
    direction = body.get('direction')
    if direction not in ('maximize', 'minimize', 'none'):
        raise ValueError('Select maximize, minimize or none')
    aggregation = body.get('aggregation', 'last')
    if aggregation not in ('last', 'best', 'mean') or aggregation == 'best' and direction == 'none':
        raise ValueError('Invalid metric aggregation')
    item = {'id': digest([project['id'], key])[:32], 'project_id': project['id'], 'key': key,
            'label': str(body.get('label', key))[:160], 'unit': str(body.get('unit', 'scalar'))[:40],
            'direction': direction, 'aggregation': aggregation, 'updated_at': time.time()}
    with service.store.connect() as db:
        service.store.put(db, 'metric_definition', item)
    return item


def summary(points, definition):
    if not points:
        return None
    values = [finite(p['value']) for p in points]
    if definition.get('aggregation') == 'mean':
        return sum(values) / len(values)
    if definition.get('aggregation') == 'best':
        return (max if definition['direction'] == 'maximize' else min)(values)
    return values[-1]


def annotate(service, run_id, body):
    if service.mode == 'readonly':
        raise ValueError('Read-only mode')
    tags = body.get('tags', [])
    note = body.get('note', '')
    if not isinstance(tags, list) or len(tags) > 30 or any(not isinstance(t, str) or not 1 <= len(t) <= 80 for t in tags):
        raise ValueError('Provide up to 30 tags of 1–80 characters')
    if not isinstance(note, str) or len(note) > 10000:
        raise ValueError('Note limit is 10,000 characters')
    with service.store.connect() as db:
        run = service.store.get(db, 'run', run_id)
        run.update(tags=sorted(set(tags)), note=note)
        service.store.put(db, 'run', run)
    return run


def evaluation(service, body):
    if service.mode == 'readonly':
        raise ValueError('Read-only mode')
    run = service.get('run', body['run_id'])
    dataset = service.get('dataset', body['dataset_id'])
    if dataset['category'] != run['category']:
        raise ValueError('Evaluation dataset category differs')
    kind = body.get('type')
    if kind not in ('classification', 'keypoints'):
        raise ValueError('Use classification/keypoints; detection has its dedicated prediction importer')
    policy = body.get('policy')
    if not isinstance(policy, dict) or not policy.get('evaluator_version') or not policy.get('split') or not policy.get('checkpoint_sha256'):
        raise ValueError('Record evaluator_version, split and checkpoint_sha256')
    if policy['split'] not in ('train', 'validation', 'test'):
        raise ValueError('Invalid split')
    import re
    if not re.fullmatch('[a-f0-9]{64}', str(policy['checkpoint_sha256'])):
        raise ValueError('Checkpoint SHA-256 must identify the evaluated weights')
    if kind == 'keypoints' and policy.get('coordinate_unit') not in ('pixels', 'normalized'):
        raise ValueError('Keypoint coordinates require pixels or normalized units')
    if kind=='keypoints' and policy.get('tolerance') is not None and finite(policy['tolerance'])<=0:
        raise ValueError('Keypoint tolerance must be positive')
    rows = body.get('samples')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10000:
        raise ValueError('Provide 1–10,000 evaluated samples per report')
    from .indexed_datasets import sample_at
    cleaned, seen = [], set()
    for row in rows:
        index = row.get('index')
        if type(index) is not int or index in seen:
            raise ValueError('Sample indexes must be distinct non-negative integers')
        seen.add(index)
        sample = sample_at(dataset, index)
        if sample['split'] != policy['split']:
            raise ValueError('Sample is outside the selected evaluation split')
        out = {'index': index, 'slice': str(row.get('slice', 'all'))[:100]}
        if kind == 'classification':
            gt, pred = row.get('target'), row.get('prediction')
            if not isinstance(gt, str) or not isinstance(pred, str) or not 1 <= len(gt) <= 100 or not 1 <= len(pred) <= 100:
                raise ValueError('Target and prediction must be nonempty class/text labels')
            confidence = finite(row.get('confidence'))
            if not 0 <= confidence <= 1:
                raise ValueError('Confidence must be 0–1')
            out.update(target=gt, prediction=pred, confidence=confidence, error=gt != pred)
        else:
            gt, pred = row.get('target'), row.get('prediction')
            if not isinstance(gt, list) or not isinstance(pred, list) or len(gt) != 2 or len(pred) != 2:
                raise ValueError('Provide target and predicted [x,y] coordinates')
            for v in gt + pred:
                finite(v)
                if policy['coordinate_unit'] == 'normalized' and not 0 <= v <= 1:
                    raise ValueError('Normalized coordinates must be 0–1')
            out.update(target=gt, prediction=pred, error=math.dist(gt, pred))
        cleaned.append(out)
    spec = {'run_id': run['id'], 'dataset': {'id': dataset['id'], 'version': dataset['version']}, 'type': kind, 'policy': policy, 'samples': cleaned}
    item = {**spec, 'id': digest(spec)[:32], 'created_at': time.time(), 'synthetic': bool(dataset.get('synthetic') or run.get('synthetic')),
            'project_id': run.get('project_id'), 'category': run['category'], 'sample_count': len(cleaned),
            'coverage': len(cleaned) / max(1, dataset['splits'].get(policy['split'], 0))}
    with service.store.connect() as db:
        existing = next((r for r in service.store.all(db, 'analysis_report') if r['id'] == item['id']), None)
        if existing:
            return existing
        service.store.put(db, 'analysis_report', item)
    return item


def analyze(report, slice_name=None, errors_only=False, offset=0, limit=50):
    if offset < 0 or not 1 <= limit <= 200:
        raise ValueError('Invalid pagination')
    rows = [r for r in report['samples'] if not slice_name or r['slice'] == slice_name]
    groups = defaultdict(list)
    for row in rows:
        groups[row['slice']].append(row)
    def metrics(values):
        if not values:
            return {'count': 0}
        errors = [float(r['error']) for r in values]
        if report['type'] == 'classification':
            return {'count': len(values), 'accuracy': 1 - sum(errors) / len(values)}
        ordered=sorted(errors)
        result={'count': len(values), 'mean_error': sum(errors) / len(errors), 'max_error': max(errors),
                'median_error':ordered[len(ordered)//2], 'p90_error':ordered[max(0,math.ceil(len(ordered)*.9)-1)]}
        if report['policy'].get('tolerance'):
            result['within_tolerance']=sum(e<=report['policy']['tolerance'] for e in errors)/len(errors)
        return result
    confusion = Counter((r['target'], r['prediction']) for r in rows) if report['type'] == 'classification' else {}
    selected = sorted((r for r in rows if not errors_only or r['error']), key=lambda r: float(r['error']), reverse=True)
    classes=[]
    if report['type']=='classification':
        for label in sorted({r[k] for r in rows for k in ('target','prediction')}):
            tp=confusion.get((label,label),0)
            support=sum(n for (a,b),n in confusion.items() if a==label)
            predicted=sum(n for (a,b),n in confusion.items() if b==label)
            precision=tp/predicted if predicted else None
            recall=tp/support if support else None
            classes.append({'class':label,'support':support,'precision':precision,'recall':recall,
                            'f1':2*tp/(support+predicted) if support+predicted else None})
    return {'report': {k: v for k, v in report.items() if k != 'samples'}, 'summary': metrics(rows),'classes':classes,
            'slices': {k: metrics(v) for k, v in groups.items()},
            'confusion': [{'target': a, 'prediction': b, 'count': n} for (a, b), n in confusion.items()],
            'items': selected[offset:offset+limit], 'total': len(selected), 'offset': offset, 'limit': limit}


def lineage(service, dataset_id):
    dataset = service.get('dataset', dataset_id)
    runs = [r for r in service.list('run', limit=100000)['items'] if any(d['id'] == dataset_id for d in r.get('frozen', {}).get('datasets') or [])]
    reports = [r for r in service.list('analysis_report', limit=100000)['items'] if r['dataset']['id'] == dataset_id]
    return {'dataset': {'id': dataset_id, 'version': dataset['version'], 'parent_id': dataset.get('parent_id')},
            'runs': [{k: r.get(k) for k in ('id', 'name', 'state', 'node_id', 'model_id', 'model_version')} for r in runs],
            'evaluations': [{k: r[k] for k in ('id', 'run_id', 'type', 'policy')} for r in reports]}
