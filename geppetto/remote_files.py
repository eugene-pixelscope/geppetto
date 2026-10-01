"""Completed MLflow artifact retrieval through the configured server, never arbitrary URIs."""
import hashlib
from pathlib import Path, PurePosixPath
import tempfile
from urllib.parse import quote
import os

import httpx


def fetch_completed(artifact,cache,url,token=None):
    uri=artifact.get('artifact_uri') or ''
    if not url or not uri.startswith('mlflow-artifacts:/'):
        raise ValueError('Only artifacts served by the configured MLflow artifact proxy are supported')
    prefix=uri.removeprefix('mlflow-artifacts:/').strip('/')
    relative=artifact['path']
    for value in (prefix,relative):
        if PurePosixPath(value).is_absolute() or '..' in PurePosixPath(value).parts or '\\' in value:
            raise ValueError('Unsafe artifact path')
    cache=Path(cache)
    cache.mkdir(parents=True,exist_ok=True)
    sha=hashlib.sha256()
    size=0
    fd,path=tempfile.mkstemp(dir=cache,suffix='.part')
    try:
        with os.fdopen(fd,'wb') as stream,httpx.stream('GET',url.rstrip('/')+'/api/2.0/mlflow-artifacts/artifacts/'+quote(prefix+'/'+relative,safe='/'),
            headers={'Authorization':'Bearer '+token} if token else {},timeout=60,follow_redirects=False) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes(1024*1024):
                stream.write(chunk)
                sha.update(chunk)
                size+=len(chunk)
        if artifact.get('size') is not None and size!=artifact['size']:
            raise ValueError('Completed artifact size changed')
        if artifact.get('sha256') and sha.hexdigest()!=artifact['sha256']:
            raise ValueError('Completed artifact content changed')
        name=sha.hexdigest()+Path(relative).suffix
        Path(path).replace(cache/name)
        return str(cache),name,{'sha256':sha.hexdigest(),'size':size}
    finally:
        Path(path).unlink(missing_ok=True)
