"""Read-only host collector and bounded node telemetry; missing readings stay null."""
import csv
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time


def quantity(value):
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)([numkKMGTPE]i?|)', str(value))
    if not match:
        raise ValueError('Invalid resource quantity')
    suffix = match[2]
    factors = {'': 1, 'n': 1e-9, 'u': 1e-6, 'm': 1e-3, 'k': 1e3, 'K': 1e3}
    factors.update({c: 1000 ** i for i, c in enumerate('MGTPE', 2)})
    factors.update({c+'i': 1024 ** i for i, c in enumerate('KMGTPE', 1)})
    return float(match[1]) * factors[suffix]


def clean(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid node telemetry')
    result = {}
    for key in ('observed_at', 'cpu_cores', 'cpu_percent', 'memory_used_bytes', 'memory_total_bytes',
                'disk_used_bytes', 'disk_total_bytes', 'network_rx_bytes_per_second', 'network_tx_bytes_per_second'):
        number = value.get(key)
        if number is None:
            result[key] = None
        elif isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number) or number < 0:
            raise ValueError('Invalid node metric: ' + key)
        else:
            result[key] = number
    result['source'] = str(value.get('source', 'unknown'))[:80]
    result['error'] = str(value.get('error', ''))[:300]
    gpus = value.get('gpus', [])
    if not isinstance(gpus, list) or len(gpus) > 128:
        raise ValueError('Invalid GPU metrics')
    result['gpus'] = []
    for gpu in gpus:
        item = {k: str(gpu.get(k, ''))[:100] for k in ('uuid', 'name')}
        for key in ('utilization_percent', 'memory_used_mib', 'memory_total_mib', 'temperature_c', 'power_watts'):
            n = gpu.get(key)
            if n is not None and (isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or n < 0):
                raise ValueError('Invalid GPU measurement')
            item[key] = n
        result['gpus'].append(item)
    return result


class HostCollector:
    def __init__(self, proc='/host/proc', root='/host/root'):
        self.proc, self.root, self.previous = Path(proc), root, None

    def sample(self):
        now = time.time()
        value = {'observed_at': now, 'source': 'node-collector', 'gpus': []}
        counters = list(map(int, (self.proc/'stat').read_text().splitlines()[0].split()[1:9]))
        total, idle = sum(counters), counters[3] + counters[4]
        memory = {line.split(':')[0]: int(line.split()[1])*1024 for line in (self.proc/'meminfo').read_text().splitlines()}
        value.update(memory_total_bytes=memory['MemTotal'], memory_used_bytes=memory['MemTotal']-memory['MemAvailable'])
        net = [line.split(':', 1) for line in (self.proc/'net/dev').read_text().splitlines()[2:]]
        rx = sum(int(row.split()[0]) for name, row in net if name.strip() != 'lo')
        tx = sum(int(row.split()[8]) for name, row in net if name.strip() != 'lo')
        if self.previous:
            at, old_total, old_idle, old_rx, old_tx = self.previous
            if total > old_total:
                value['cpu_percent'] = 100 * (1 - (idle-old_idle)/(total-old_total))
            if now > at and rx >= old_rx and tx >= old_tx:
                value.update(network_rx_bytes_per_second=(rx-old_rx)/(now-at), network_tx_bytes_per_second=(tx-old_tx)/(now-at))
        self.previous = now, total, idle, rx, tx
        try:
            stat = os.statvfs(self.root)
            value.update(disk_total_bytes=stat.f_blocks*stat.f_frsize, disk_used_bytes=(stat.f_blocks-stat.f_bfree)*stat.f_frsize)
        except OSError:
            pass
        try:
            output = subprocess.run(['nvidia-smi', '--query-gpu=uuid,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw',
                                     '--format=csv,noheader,nounits'], check=True, capture_output=True, text=True, timeout=3).stdout
            for row in csv.reader(io.StringIO(output)):
                gpu = {'uuid': row[0].strip(), 'name': row[1].strip()}
                for key, raw in zip(('utilization_percent', 'memory_used_mib', 'memory_total_mib', 'temperature_c', 'power_watts'), row[2:]):
                    try:
                        gpu[key] = float(raw.strip())
                    except ValueError:
                        gpu[key] = None
                value['gpus'].append(gpu)
        except (OSError, subprocess.SubprocessError):
            value['error'] = 'GPU collector is unavailable'
        return clean(value)


def collect_inventory(inventory, nodes):
    """Telemetry failures must not turn healthy inventory into a disconnected cluster."""
    import httpx
    from urllib.parse import quote
    now = time.time()
    by_name = {n['name']: n for n in nodes}
    for node in nodes:
        node['telemetry'] = {'source': 'unavailable', 'error': 'Connect a usage collector', 'observed_at': None}
    try:
        for metric in inventory.pages('/apis/metrics.k8s.io/v1beta1/nodes'):
            node = by_name.get(metric['metadata']['name'])
            if node:
                from datetime import datetime
                at = datetime.fromisoformat(metric['timestamp'].replace('Z', '+00:00')).timestamp()
                cpu = quantity(metric['usage']['cpu'])
                capacity = quantity(node['capacity'].get('cpu', '0'))
                node['telemetry'] = {'source': 'metrics-server', 'observed_at': at, 'cpu_cores': cpu,
                    'cpu_percent': cpu/capacity*100 if capacity else None,
                    'memory_used_bytes': quantity(metric['usage']['memory']),
                    'memory_total_bytes': quantity(node['capacity'].get('memory', '0')) or None}
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    try:
        pods = inventory.pages('/api/v1/pods')
        for node in nodes:
            assigned = [p for p in pods if p.get('spec', {}).get('nodeName') == node['name'] and p.get('status', {}).get('phase') not in ('Succeeded', 'Failed')]
            allocation = {'observed_at': now, 'cpu': 0, 'memory': 0, 'gpu': 0, 'pods': len(assigned), 'managed_gpu': 0, 'run_gpus': {}}
            for pod in assigned:
                spec = pod.get('spec', {})
                for key, target in [('cpu', 'cpu'), ('memory', 'memory'), ('nvidia.com/gpu', 'gpu')]:
                    def request(c):
                        resources = c.get('resources', {})
                        return quantity(resources.get('requests', {}).get(key, resources.get('limits', {}).get(key, '0')))
                    app = sum(request(c) for c in spec.get('containers', []))
                    sidecars, init = 0, 0
                    for container in spec.get('initContainers', []):
                        if container.get('restartPolicy') == 'Always': sidecars += request(container)
                        init = max(init, sidecars + (0 if container.get('restartPolicy') == 'Always' else request(container)))
                    amount = max(app+sidecars, init) + quantity(spec.get('overhead', {}).get(key, '0'))
                    allocation[target] += amount
                    if target == 'gpu' and pod['metadata'].get('labels', {}).get('app.kubernetes.io/managed-by') == 'geppetto-agent':
                        allocation['managed_gpu'] += amount
                        run_id = pod['metadata'].get('labels', {}).get('geppetto.run-id', '').split('-n')[0]
                        allocation['run_gpus'][run_id] = allocation['run_gpus'].get(run_id, 0) + amount
            node['allocation'] = allocation
        for pod in pods:
            if pod['metadata'].get('labels', {}).get('app') != 'geppetto-node-metrics' or pod.get('status', {}).get('phase') != 'Running':
                continue
            node = by_name.get(pod['spec'].get('nodeName'))
            if not node:
                continue
            ns, name = pod['metadata']['namespace'], pod['metadata']['name']
            try:
                value = inventory.get('/api/v1/namespaces/'+quote(ns, safe='')+'/pods/'+quote(name, safe='')+':9108/proxy/metrics')
                node['telemetry'] = clean(value)
            except (httpx.HTTPError, ValueError, KeyError):
                pass
    except (httpx.HTTPError, ValueError, KeyError):
        pass


def view(service, id):
    node = service.get('whale', id)
    from .resource_policy import held
    runs = [r for r in service.list('run', limit=100000)['items'] if id in (r.get('frozen', {}).get('gpu', {}).get('node_ids') or [r.get('node_id')])]
    telemetry = node.get('node', {}).get('telemetry', {})
    return {'node_id': id, 'telemetry': telemetry, 'stale': not telemetry.get('observed_at') or time.time()-telemetry['observed_at'] > 60,
            'capacity': node.get('node', {}).get('capacity', {}), 'allocatable': node.get('node', {}).get('allocatable', {}),
            'allocation': node.get('node', {}).get('allocation'), 'history': node.get('resource_history', []),
            'runs': [{'id': r['id'], 'name': r['name'], 'project_id': r.get('project_id'), 'state': r['state'],
                      'gpu_count': r.get('frozen', {}).get('gpu', {}).get('count', 1), 'held': held(r), 'phase': r.get('phase')}
                     for r in runs if held(r) or r['state'] not in ('succeeded', 'failed', 'stopped', 'skipped')]}


def main():
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    collector = HostCollector(os.getenv('GEPPETTO_HOST_PROC', '/host/proc'), os.getenv('GEPPETTO_HOST_ROOT', '/host/root'))
    current = {}
    def update():
        while True:
            try:
                current['value'] = collector.sample()
            except (OSError, ValueError, KeyError):
                current['value'] = {'source': 'node-collector', 'observed_at': None, 'error': 'Node measurement failed'}
            time.sleep(5)
    threading.Thread(target=update, daemon=True).start()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/metrics':
                self.send_error(404); return
            payload = json.dumps(current.get('value', {})).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
        def log_message(self, *args):
            pass
    HTTPServer(('0.0.0.0', 9108), Handler).serve_forever()


if __name__ == '__main__':
    main()


def sample_local(service, collector):
    """The local-workspace node represents this server, never a remote registered host."""
    from .workshop import LOCAL_WHALE
    metric = collector.sample()
    with service.store.connect() as db:
        nodes = service.store.all(db, 'whale')
        node = next((n for n in nodes if n['id'] == LOCAL_WHALE and n['connection'] == 'local-workspace'), None)
        if not node:
            return
        node.setdefault('node', {}).update(telemetry=metric, capacity={'cpu': str(os.cpu_count()),
            'memory': str(int(metric['memory_total_bytes'])), 'nvidia.com/gpu': str(len(metric['gpus']))})
        history = node.setdefault('resource_history', [])
        history.append(metric)
        node['resource_history'] = history[-240:]
        service.store.put(db, 'whale', node)
