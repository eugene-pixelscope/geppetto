"""Geppetto tracking client. Import as ``import geppetto_sdk as geppetto``."""
import base64
import hashlib
import json
import os
from pathlib import Path
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler
import uuid
import warnings

__version__ = '0.3.0'


class TrackingError(RuntimeError):
    """Tracking was not acknowledged; training state has not been inferred."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TrackingError('Tracking redirects are not allowed')


class _Client:
    def __init__(self, url, api_key, ca_cert, timeout):
        self.url = (url or os.environ.get('GEPPETTO_URL', '')).rstrip('/')
        self.key = api_key or os.environ.get('GEPPETTO_API_KEY', '')
        parsed = urlsplit(self.url)
        if (parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path):
            raise ValueError('GEPPETTO_URL must be an HTTP(S) origin')
        if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('HTTPS is required outside loopback')
        if not self.key:
            raise ValueError('Set GEPPETTO_API_KEY to a project SDK token')
        self.timeout = timeout
        context = ssl.create_default_context(cafile=ca_cert or os.environ.get('GEPPETTO_CA_CERT') or None)
        self.opener = build_opener(_NoRedirect(), HTTPSHandler(context=context))

    def post(self, path, body):
        body = dict(body)
        body.setdefault('request_id', uuid.uuid4().hex)
        payload = json.dumps(body, allow_nan=False).encode()
        for attempt in range(3):
            request = Request(self.url + '/api/tracking/' + path, data=payload, method='POST', headers={
                'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json',
                'X-Geppetto-Request': '1', 'User-Agent': 'geppetto-sdk/' + __version__})
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    return json.load(response)
            except HTTPError as exc:
                if exc.code not in (429, 502, 503, 504) or attempt == 2:
                    try:
                        detail = json.loads(exc.read()).get('detail', 'Request rejected')
                    except (ValueError, OSError):
                        detail = 'Request rejected'
                    raise TrackingError('HTTP %s: %s' % (exc.code, detail)) from None
            except (URLError, TimeoutError, OSError):
                if attempt == 2:
                    raise TrackingError('Cannot reach Geppetto; event delivery is unconfirmed') from None
            time.sleep(.5 * 2 ** attempt)


class Run:
    """A single training/evaluation execution, associated with one model."""
    def __init__(self, client, info, heartbeat_interval):
        self._client = client
        self.id, self.model_id, self.project_id = info['id'], info['model_id'], info['project_id']
        self.state = info['state']
        self.enabled = True
        self._step = info.get('last_step', -1)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self.last_error = None
        if heartbeat_interval > 0 and self.state == 'running':
            self._thread = threading.Thread(target=self._heartbeat, args=(heartbeat_interval,), daemon=True)
            self._thread.start()

    def _event(self, kind, **body):
        with self._lock:
            if self.state != 'running':
                raise TrackingError('Run is already finished')
            result = self._client.post('runs/' + self.id + '/events', dict(kind=kind, **body))
            self.state = result['state']
            return result

    def _heartbeat(self, interval):
        while not self._stop.wait(interval):
            try:
                self._event('heartbeat')
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                warnings.warn('Geppetto heartbeat failed: ' + str(exc), RuntimeWarning)

    def log(self, metrics, *, step=None, epoch=None):
        """Append finite numeric metrics. Step is monotonic; epoch is explicit."""
        with self._lock:
            step = self._step + 1 if step is None else step
            result = self._event('log', metrics=metrics, step=step, epoch=epoch)
            self._step = step
            return result

    def progress(self, epoch, total_epochs, *, phase='training'):
        return self._event('progress', epoch=epoch, total_epochs=total_epochs, phase=phase)

    def log_evaluation(self, *, dataset_id, type, policy, samples):
        """Publish classification/keypoint predictions with an explicit evaluation policy.

        policy identifies evaluator_version, split and checkpoint_sha256; keypoints
        also require coordinate_unit. Samples reference indexes in the dataset version.
        This records supplied predictions; it does not run inference or change weights.
        """
        with self._lock:
            return self._client.post('runs/' + self.id + '/evaluation', dict(
                dataset_id=dataset_id, type=type, policy=policy, samples=samples))

    def log_histogram(self, name, values, *, step, bins=32):
        """Record finite samples as explicit histogram bins."""
        import math
        values=list(values)
        if not values or not 1<=bins<=256 or any(not math.isfinite(v) for v in values):
            raise ValueError('Finite nonempty values and 1–256 bins required')
        lo,hi=min(values),max(values)
        if lo==hi:lo-=.5;hi+=.5
        width=(hi-lo)/bins
        counts=[0]*bins
        for value in values:counts[min(bins-1,int((value-lo)/width))]+=1
        return self._client.post('runs/'+self.id+'/visualizations',dict(type='histogram',name=name,step=step,
            data=dict(edges=[lo+i*width for i in range(bins+1)],counts=counts)))

    def log_embedding(self, name, points, *, step, projection='coordinates'):
        """Publish explicit 2D projections with optional point labels."""
        return self._client.post('runs/'+self.id+'/visualizations',dict(type='embedding',name=name,step=step,
            data=dict(points=list(points),projection=projection)))

    def log_trace(self, name, events, *, step=0):
        """Publish profiler duration events: name, lane, start_us, duration_us."""
        return self._client.post('runs/'+self.id+'/visualizations',dict(type='trace',name=name,step=step,
            data=dict(events=list(events))))

    def log_artifact(self, path, *, role='checkpoint'):
        """Upload file bytes, publishing only after complete size/SHA-256 verification."""
        path = Path(path)
        with self._lock, path.open('rb') as stream:
            if self.state != 'running':
                raise TrackingError('Upload artifacts before finishing the run')
            checksum = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                checksum.update(chunk)
            size = stream.tell()
            stream.seek(0)
            key = uuid.uuid4().hex
            endpoint = 'runs/' + self.id + '/artifacts'
            result = self._client.post(endpoint, dict(request_id=key, action='start', name=path.name,
                                      size=size, sha256=checksum.hexdigest(), role=role))
            offset = result['offset']
            stream.seek(offset)
            while offset < size:
                data = stream.read(min(1024 * 1024, size - offset))
                if not data:
                    raise TrackingError('Artifact changed during upload')
                result = self._client.post(endpoint, dict(request_id=key, action='chunk', offset=offset,
                                          data=base64.b64encode(data).decode('ascii')))
                offset = result['offset']
            return self._client.post(endpoint, dict(request_id=key, action='complete'))

    def finish(self, state='succeeded', *, message=None):
        with self._lock:
            if self.state != 'running':
                if self.state != state:
                    raise TrackingError('Run already finished with a different state')
                return
            result = self._event('finish', state=state, message=message)
            self._stop.set()
        if self._thread and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1)
        return result

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.state == 'running':
                self.finish('failed' if exc_type else 'succeeded', message=str(exc)[:2000] if exc else None)
        except Exception:
            if exc_type is None:
                raise
            warnings.warn('Geppetto could not record failure; original exception is preserved', RuntimeWarning)
        finally:
            self._stop.set()
        return False


class _DisabledRun:
    enabled = False
    id = model_id = project_id = None
    state = 'disabled'
    last_error = None
    def log(self, *args, **kwargs): pass
    def progress(self, *args, **kwargs): pass
    def log_artifact(self, *args, **kwargs): pass
    def log_evaluation(self, *args, **kwargs): pass
    def log_histogram(self, *args, **kwargs): pass
    def log_embedding(self, *args, **kwargs): pass
    def log_trace(self, *args, **kwargs): pass
    def finish(self, *args, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): return False


def init(*, project=None, name=None, architecture=None, config=None, dataset_id=None, node_id=None,
         job_type='train', parent_run_id=None, url=None, api_key=None, ca_cert=None,
         enabled=True, heartbeat_interval=30, timeout=10):
    """Register execution. Nonzero distributed RANK is automatically disabled.

    For resume/evaluate, supply parent_run_id; checkpoint loading remains in training code.
    Credentials are never included in config or sent as metrics.
    """
    if not enabled or os.environ.get('RANK', '-1') not in ('-1', '0'):
        return _DisabledRun()
    if heartbeat_interval < 0 or timeout <= 0:
        raise ValueError('Invalid timing options')
    client = _Client(url, api_key, ca_cert, timeout)
    body = dict(project=project, name=name, architecture=architecture, dataset_id=dataset_id,
                node_id=node_id, job_type=job_type, parent_run_id=parent_run_id)
    if config is not None:
        body['config'] = config
    return Run(client, client.post('runs', body), heartbeat_interval)


def attach(run_id, *, url=None, api_key=None, ca_cert=None, heartbeat_interval=30, timeout=10):
    """Reconnect to an existing SDK run, without creating a model or loading weights.

    Only attach after confirming the original writer has stopped. This can mark
    a killed process failed before init(job_type='resume', parent_run_id=...).
    """
    if os.environ.get('RANK', '-1') not in ('-1', '0'):
        return _DisabledRun()
    if not isinstance(run_id, str) or not run_id.startswith('sdk-') or len(run_id) != 36 or any(c not in '0123456789abcdef' for c in run_id[4:]):
        raise ValueError('Invalid SDK run ID')
    client = _Client(url, api_key, ca_cert, timeout)
    info = client.post('runs/' + run_id + '/inspect', {})
    return Run(client, info, heartbeat_interval)
