"""Mutating demo smoke tests must use a separate loopback catalog."""
import json
import os
from urllib.parse import urlparse
from urllib.request import urlopen
BASE=os.environ.get('GEPPETTO_BROWSER_BASE','http://127.0.0.1:8093').rstrip('/')
def require_demo():
    parsed=urlparse(BASE)
    if parsed.hostname not in ('127.0.0.1','localhost') or parsed.port==8091:
        raise RuntimeError('Use an isolated demo server on a port other than the user catalog (8091)')
    with urlopen(BASE+'/api/status') as response:status=json.load(response)
    if status['mode']!='demo':raise RuntimeError('This smoke test writes synthetic records; an isolated demo workspace is required')
