"""Pinned single-image explanations dispatched through the existing Agent queue."""
import os
import re
import time
import uuid
from pathlib import Path
from .configuration import digest
from .files import fingerprint
from .indexed_datasets import sample_at
from .model_history import ensure_visible
from .service import TERMINAL


def options(service, run_id):
    source = service.get('run', run_id)
    checkpoints = [a for a in source.get('artifacts', []) if a.get('complete') and a.get('role') == 'inference_weights' and a.get('artifact_uri', '').startswith('mlflow-artifacts:/')]
    templates = {t['id'] for t in service.list('template', limit=100000)['items'] if t.get('adapter') == 'ball'}
    profiles = [p for p in service.list('execution_profile', limit=100000)['items'] if p['enabled'] and p['template_id'] in templates]
    datasets = [dict(id=d['id'], name=d['name'], splits=d['splits'], version=d['version']) for d in service.list('dataset', limit=100000)['items'] if d['category'] == 'ball-detection' and d.get('content_verified') and not d.get('synthetic') and d.get('locations')]
    from .run_history import page
    runs = [r for r in page(service, limit=100000)['items'] if r.get('source_kind') == 'gradcam' and r.get('frozen', {}).get('parent_id') == run_id]
    return {'checkpoints': checkpoints, 'profiles': profiles, 'datasets': datasets, 'runs': runs}


def preview(service, home, run_id, body):
    if service.mode in ('demo', 'readonly'):
        raise ValueError('Grad-CAM requires a live Agent execution profile')
    source = service.get('run', run_id)
    if source['category'] != 'ball-detection' or source['state'] not in TERMINAL:
        raise ValueError('Select a finalized detection model')
    with service.store.connect() as db:
        ensure_visible(service.store, db, source['pinocchio_id'])
    profile = service.get('execution_profile', body['profile_id'])
    template = service.get('template', profile['template_id'])
    if template.get('adapter') != 'ball':
        raise ValueError('Only the reviewed YOLO detection adapter is supported')
    dataset = service.get('dataset', body['dataset_id'])
    index = body.get('index')
    size = body.get('imgsz', 960)
    rank = body.get('target_rank', 1)
    if type(index) is not int or index < 0 or type(size) is not int or size not in (640, 960, 1280, 1920) or type(rank) is not int or not 1 <= rank <= 20:
        raise ValueError('Invalid sample index, input size or detection rank')
    sample = sample_at(dataset, index)
    if sample['split'] not in ('validation', 'test'):
        raise ValueError('Select a validation or test image')
    expected = sample.get('image_file')
    if not expected or fingerprint(dataset['root'], sample['image']) != expected:
        raise ValueError('Selected image differs from its registered content version')
    # Validate current Agent/PVC bindings and config using the normal execution freezer.
    frozen = service.freeze({'node_id': profile['node_id'], 'template_id': template['id'], 'dataset_ids': [dataset['id']], 'overrides': {'imgsz': size, 'batch': 1}})
    checkpoint = next((a for a in source['artifacts'] if a['id'] == body.get('checkpoint_id') and a.get('complete') and a.get('role') == 'inference_weights'), None)
    if not checkpoint:
        raise ValueError('Select completed inference weights')
    from .remote_files import fetch_completed
    cache_id = digest({'source': run_id, 'artifact': checkpoint})
    try:
        pin = service.get('gradcam_checkpoint', cache_id)
    except KeyError:
        _, _, pin = fetch_completed(checkpoint, Path(home)/'artifact-cache', os.environ.get('GEPPETTO_MLFLOW_URL'), os.environ.get('GEPPETTO_MLFLOW_TOKEN'))
        pin['id'] = cache_id
        with service.store.connect() as db:
            service.store.put(db, 'gradcam_checkpoint', pin)
    from .execution import relative
    policy = {'method': 'gradcam', 'version': 1, 'checkpoint_uri': checkpoint['artifact_uri'].rstrip('/')+'/'+relative(checkpoint['path']), 'checkpoint_sha256': pin['sha256'], 'sample': {'index': index, 'image': sample['image'], 'split': sample['split'], 'image_file': expected}, 'class_id': 0, 'target_rank': rank, 'precision': 'fp32', 'nms_iou': .7}
    frozen.update(pinocchio_id=source['pinocchio_id'], project_id=source.get('project_id'), model_name=source['name'], relation='evaluate', parent_id=run_id, evaluation=policy, evaluation_policy=[policy])
    return {'frozen': frozen, 'confirmation_hash': digest(frozen)}


def submit(service, home, run_id, body, key):
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', key):
        raise ValueError('Idempotency-Key is required')
    request_hash = digest({'run_id': run_id, 'body': body})
    def existing(db):
        row = db.execute('SELECT digest,kind,id FROM requests WHERE key=?', (key,)).fetchone()
        if row:
            if row[0] != request_hash:
                raise ValueError('Idempotency conflict')
            return service.store.get(db, row[1], row[2])
    with service.store.connect() as db:
        prior = existing(db)
        if prior:
            return prior
    result = preview(service, home, run_id, body)
    if result['confirmation_hash'] != body.get('confirmation_hash'):
        raise ValueError('Settings have changed. Review the preview again.')
    frozen = result['frozen']
    run = service.new_run({'id': uuid.uuid4().hex, 'name': 'Grad-CAM · '+frozen['evaluation']['sample']['image'].split('/')[-1], 'category': 'ball-detection', 'source_kind': 'gradcam', 'template_id': frozen['template']['id'], 'node_id': frozen['node_id'], 'pinocchio_id': frozen['pinocchio_id'], 'model_id': frozen['pinocchio_id'], 'project_id': frozen['project_id'], 'frozen': frozen, 'created_at': time.time(), 'synthetic': False, 'priority': 0})
    with service.store.connect() as db:
        prior = existing(db)
        if prior:
            return prior
        ensure_visible(service.store, db, frozen['pinocchio_id'])
        profile = frozen['execution']['profile']
        if service.store.get(db, 'execution_profile', profile['id']) != profile:
            raise ValueError('Execution profile changed; preview again')
        dataset = service.store.get(db, 'dataset', frozen['datasets'][0]['id'])
        if dataset['version'] != frozen['datasets'][0]['version'] or frozen['execution']['locations'][0] not in dataset.get('locations', []):
            raise ValueError('Dataset binding changed; preview again')
        service.store.put(db, 'run', run)
        db.execute('INSERT INTO requests VALUES(?,?,?,?)', (key, request_hash, 'run', run['id']))
    return run
