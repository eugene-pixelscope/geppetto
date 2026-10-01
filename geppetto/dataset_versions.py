"""Create immutable content versions from indexed datasets in a background worker."""
import copy
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid
from .configuration import digest
from .files import fingerprint, open_beneath
from .indexed_datasets import sample_page


def request(service, dataset_id):
    if service.mode == 'readonly':
        raise ValueError('Read-only mode')
    dataset = service.get('dataset', dataset_id)
    from .files import allowed_root
    allowed_root(dataset['root'],service.roots)
    with service.store.connect() as db:
        old = next((j for j in service.store.all(db, 'dataset_verification') if j['dataset_id'] == dataset_id and j['state'] in ('queued', 'running')), None)
        if old:
            return old
        job = {'id': uuid.uuid4().hex, 'dataset_id': dataset_id, 'state': 'queued', 'checked': 0,
               'total': dataset.get('sample_count', len(dataset.get('samples', []))), 'created_at': time.time()}
        service.store.put(db, 'dataset_verification', job)
    return job


def tick(service, home):
    jobs = service.list('dataset_verification', limit=100000)['items']
    job = next((j for j in jobs if j['state'] in ('queued', 'running')), None)
    if not job:
        return
    dataset = service.get('dataset', job['dataset_id'])
    folder = Path(home) / 'content-versions' / job['id']
    folder.mkdir(parents=True, exist_ok=True)
    index = folder / 'samples.sqlite3'
    with sqlite3.connect(index) as db:
        db.execute('CREATE TABLE IF NOT EXISTS samples (idx INTEGER PRIMARY KEY, image TEXT UNIQUE, label TEXT, split TEXT, record TEXT, content TEXT)')
        db.execute('CREATE INDEX IF NOT EXISTS samples_split ON samples(split,idx)')
        db.execute('CREATE INDEX IF NOT EXISTS samples_content ON samples(content)')
        checked = db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
    try:
        page = sample_page(dataset, offset=checked, limit=4096)
        with sqlite3.connect(index) as db:
            for row in page['items']:
                image = fingerprint(dataset['root'], row['image'])
                label = fingerprint(dataset['root'], row['label']) if row.get('label') else None
                if dataset.get('policy',{}).get('format') in ('yolo','yolo-pose'):
                    from PIL import Image
                    with open_beneath(dataset['root'],row['image']) as stream:
                        with Image.open(stream) as image_file:image_file.verify()
                    if not label:raise ValueError('YOLO sample requires an explicit label file (empty for negative images)')
                    with open_beneath(dataset['root'],row['label']) as stream:
                        for line in stream.read().decode().splitlines():
                            if not line.strip():continue
                            values=[float(v) for v in line.split()]
                            if (len(values)<5 or any(not math.isfinite(v) for v in values) or values[0]<0 or not values[0].is_integer()
                                or not all(0<=v<=1 for v in values[1:5]) or not all(v>0 for v in values[3:5])):
                                raise ValueError('Invalid YOLO box label: '+row['label'])
                    if label!=fingerprint(dataset['root'],row['label']) or image!=fingerprint(dataset['root'],row['image']):
                        raise ValueError('Sample changed during validation')
                # Exact duplicate image bytes spanning splits invalidate the version.
                conflict = db.execute('SELECT split FROM samples WHERE content=? AND split<>? LIMIT 1', (image['sha256'], row['split'])).fetchone()
                if conflict:
                    raise ValueError('Identical image content appears in different splits: ' + row['image'])
                record = {'index': row['index'], 'image': row['image'], 'label': row.get('label'), 'split': row['split'],
                          'image_file': image, 'label_file': label}
                db.execute('INSERT INTO samples VALUES(?,?,?,?,?,?)', (row['index'], row['image'], row.get('label'), row['split'], json.dumps(record, sort_keys=True), image['sha256']))
        checked += len(page['items'])
        job.update(state='running', checked=checked, total=page['total'], updated_at=time.time())
        if checked == page['total']:
            sources = [fingerprint(dataset['root'], s['path']) for s in dataset.get('source_files', [])]
            manifest = folder / 'manifest.jsonl'
            sha = hashlib.sha256()
            with sqlite3.connect(index) as db, manifest.open('wb') as out:
                for (record,) in db.execute('SELECT record FROM samples ORDER BY idx'):
                    raw = (record + '\n').encode()
                    out.write(raw)
                    sha.update(raw)
            version = digest({'manifest_sha256': sha.hexdigest(), 'policy': dataset.get('policy', {}), 'source_files': sources})
            item = copy.deepcopy(dataset)
            item.update(id='content-' + version[:32], version=version, parent_id=dataset['id'],
                        manifest_path=str(manifest), manifest_sha256=sha.hexdigest(), sample_index=str(index),
                        source_files=sources, status='registered', content_verified=True, version_basis='content-manifest',
                        created_at=time.time(), locations=[], verification_note='All image and label content hashes and cross-split duplicate images were checked. Validation is repeated at execution.')
            with sqlite3.connect(index) as db:
                item['samples'] = [json.loads(r[0]) for r in db.execute('SELECT record FROM samples ORDER BY idx LIMIT 12')]
            with service.store.connect() as db:
                if not any(d['id'] == item['id'] for d in service.store.all(db, 'dataset')):
                    service.store.put(db, 'dataset', item)
            job.update(state='succeeded', result_id=item['id'], finished_at=time.time())
    except (OSError, ValueError, sqlite3.Error) as exc:
        job.update(state='failed', error=str(exc)[:1000], finished_at=time.time())
    with service.store.connect() as db:
        service.store.put(db, 'dataset_verification', job)


def export_registry(service, dataset_id, output, container_root):
    """Export a reviewed metadata package; never copy or modify source data."""
    dataset = copy.deepcopy(service.get('dataset', dataset_id))
    if dataset.get('status') == 'indexed' or dataset.get('content_verified') is False:
        raise ValueError('Full content verification is required')
    if not str(container_root).startswith('/datasets/') or '..' in Path(container_root).parts:
        raise ValueError('Container data must be inside /datasets/')
    from .files import iter_samples
    dataset['samples'] = list(iter_samples(dataset))
    dataset['root'] = str(container_root)
    if dataset.get('manifest_sha256'):
        dataset['content_manifest_sha256']=dataset['manifest_sha256']
    for key in ('sample_index', 'manifest_path', 'manifest_sha256', 'source_splits', 'locations'):
        dataset.pop(key, None)
    output = Path(output)
    registry = json.loads(output.read_text()) if output.exists() else {}
    registry[dataset_id] = dataset
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix('.tmp')
    temp.write_text(json.dumps(registry, ensure_ascii=False))
    temp.replace(output)
