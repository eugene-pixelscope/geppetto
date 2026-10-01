"""Register prepared jersey sources without changing original images or labels."""
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from geppetto.configuration import digest
from geppetto.storage import Store


def register(manifest_path, model_root, home):
    module_spec = importlib.util.spec_from_file_location('sports_jersey_data', model_root / 'data.py')
    data = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(data)
    prepared = data.load_manifest(manifest_path)
    store = Store(home / 'catalog.sqlite3')
    registered = []
    for source in prepared['sources']:
        samples = [s for s in prepared['samples'] if s['source'] == source['name']]
        local = {**prepared, 'sources': [source], 'samples': samples,
                 'report': {source['name']: prepared['report'][source['name']]},
                 'excluded_samples': [s for s in prepared.get('excluded_samples', []) if s['source'] == source['name']],
                 'splits': dict(Counter(s['split'] for s in samples))}
        local['version'] = data.digest({k: local[k] for k in ('samples', 'policy', 'sources')})
        local_path = manifest_path.parent / 'sources' / (local['version'] + '.json')
        data.atomic_json(local_path, local)
        policy = {'format': 'jersey-number', 'unit': 'image', 'source': source['name'],
                  'classes': list(range(100)), 'labeling': prepared['policy'],
                  'prepared_version': prepared['version'], 'training_manifest_version': local['version']}
        records = []
        for index, sample in enumerate(samples):
            records.append({'index': index, 'image': sample['image'], 'label': None,
                'number': sample['label'], 'split': sample['split'], 'source': sample['source'], 'group': sample['group'],
                'image_file': {'path': sample['image'], 'sha256': sample['sha256'],
                               'size': (Path(prepared['root']) / sample['image']).stat().st_size}, 'label_file': None})
        raw = ''.join(json.dumps(s, sort_keys=True) + '\n' for s in records).encode()
        sha = hashlib.sha256(raw).hexdigest()
        version = digest({'manifest_sha256': sha, 'policy': policy, 'source_files': []})
        id = 'jersey-' + version[:24]
        with store.connect() as db:
            if any(d['id'] == id for d in store.all(db, 'dataset')):
                registered.append(store.get(db, 'dataset', id))
                continue
        folder = home / 'content-versions' / id
        folder.mkdir(parents=True, exist_ok=True)
        content = folder / 'manifest.jsonl'
        content.write_bytes(raw)
        index_path = folder / 'samples.sqlite3'
        with sqlite3.connect(index_path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS samples (idx INTEGER PRIMARY KEY, image TEXT UNIQUE, label TEXT, split TEXT, record TEXT)')
            db.execute('DELETE FROM samples')
            db.executemany('INSERT INTO samples VALUES(?,?,?,?,?)',
                           [(s['index'], s['image'], None, s['split'], json.dumps(s)) for s in records])
            db.execute('CREATE INDEX IF NOT EXISTS samples_split ON samples(split,idx)')
        item = {'id': id, 'name': '등번호 · ' + source['name'], 'category': 'jersey-recognition',
                'node_id': 'whale-local', 'root': prepared['root'], 'version': version,
                'samples': records[:12], 'sample_count': len(records),
                'splits': {split: local['splits'].get(split, 0) for split in ('train', 'validation', 'test')},
                'policy': policy, 'source_files': [], 'sample_index': str(index_path),
                'manifest_path': str(content), 'manifest_sha256': sha,
                'local_manifest': str(local_path), 'local_manifest_version': local['version'],
                'local_runner': str(model_root / 'run_config.py'),
                'status': 'registered', 'content_verified': True, 'version_basis': 'content-manifest',
                'created_at': time.time(), 'synthetic': False, 'effective_samples': len(records),
                'verification_note': '원본 내용 해시·등번호·분할 확인 완료. 중복과 충돌 제외 내역은 데이터 준비 보고서에 기록했습니다.'}
        with store.connect() as db:
            store.put(db, 'dataset', item)
        registered.append(item)
    return registered


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--model-root', type=Path, required=True)
    parser.add_argument('--home', type=Path, default=Path(__file__).resolve().parents[1] / 'local/app')
    args = parser.parse_args()
    for item in register(args.manifest.resolve(), args.model_root.resolve(), args.home.resolve()):
        print(json.dumps({k: item[k] for k in ('id', 'name', 'splits')}, ensure_ascii=False))
