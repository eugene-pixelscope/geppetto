"""Content manifests and completed-file access inside configured roots."""
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import json


def open_beneath(root, relative):
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or any(p in ('.', '..') for p in parts):
        raise ValueError('Invalid relative path')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        result = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        if not stat.S_ISREG(os.fstat(result).st_mode):
            os.close(result)
            raise ValueError('Expected regular file')
        return os.fdopen(result, 'rb')
    finally:
        os.close(fd)


def fingerprint(root, relative):
    with open_beneath(root, relative) as stream:
        before = os.fstat(stream.fileno())
        sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('File changed during hashing')
        return {'path': relative, 'sha256': sha, 'size': after.st_size}


def manifest(root, samples):
    if not samples or len(samples) > 1000000:
        raise ValueError('Provide 1–1,000,000 explicit samples')
    seen = set()
    cache = {}
    def hashed(path):
        if path not in cache:
            cache[path]=fingerprint(root,path)
        return cache[path]
    result = []
    for sample in samples:
        if sample.get('split') not in ('train', 'validation', 'test'):
            raise ValueError('Explicit split is required')
        if sample['image'] in seen:
            raise ValueError('Duplicate image across splits')
        seen.add(sample['image'])
        result.append({**sample, 'image_file': hashed(sample['image']),
                       'label_file': hashed(sample['label']) if sample.get('label') else None})
    return result


def iter_samples(dataset):
    if dataset.get('manifest_path'):
        path=Path(dataset['manifest_path'])
        with open_beneath(path.parent,path.name) as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=dataset['manifest_sha256']:
                raise ValueError('Content manifest changed')
            stream.seek(0)
            for line in stream:
                yield json.loads(line)
    else:
        yield from dataset['samples']


def verify_manifest(dataset):
    if dataset.get('status')=='indexed' or dataset.get('content_verified') is False:
        raise ValueError('Dataset requires full content verification')
    if dataset.get('version_basis')=='content-inline':
        from .configuration import digest
        if digest({'samples':dataset['samples'],'source_files':dataset.get('source_files',[]),'policy':dataset.get('policy',{})})!=dataset['version']:
            raise ValueError('Content version identity changed')
    if dataset.get('version_basis')=='content-manifest':
        from .configuration import digest
        expected=dataset.get('manifest_sha256') or dataset.get('content_manifest_sha256')
        actual=hashlib.sha256()
        for sample in iter_samples(dataset):
            actual.update((json.dumps(sample,sort_keys=True)+'\n').encode())
        if actual.hexdigest()!=expected or digest({'manifest_sha256':expected,'policy':dataset.get('policy',{}),
                                                  'source_files':dataset.get('source_files',[])})!=dataset['version']:
            raise ValueError('Content version identity changed')
    seen=set()
    for sample in iter_samples(dataset):
        for key in ('image_file', 'label_file'):
            expected = sample.get(key)
            if expected and expected['path'] not in seen:
                if fingerprint(dataset['root'], expected['path']) != expected:
                    raise ValueError(f'Dataset changed: {expected["path"]}')
                seen.add(expected['path'])
    for expected in dataset.get('source_files',[]):
        if fingerprint(dataset['root'],expected['path'])!=expected:
            raise ValueError(f'Dataset source changed: {expected["path"]}')


def allowed_root(value, roots):
    path = Path(value).resolve(strict=True)
    if not path.is_dir() or not any(path.is_relative_to(Path(root).resolve()) for root in roots):
        raise ValueError('Dataset path is outside configured registration roots')
    return str(path)
