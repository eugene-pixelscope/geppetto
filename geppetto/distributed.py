"""Multi-node reservations and explicit, pinned per-node execution contracts."""
import copy
from .configuration import digest


def freeze(service, frozen):
    gpu = frozen['gpu']
    ids = gpu.get('node_ids') or [frozen['node_id']]
    if not isinstance(ids, list) or not 1 <= len(ids) <= 16 or len(set(ids)) != len(ids) or ids[0] != frozen['node_id']:
        raise ValueError('Select distinct nodes with the primary node first (maximum 16)')
    if len(ids) == 1:
        return
    if frozen['template']['adapter'] != 'ball' or frozen['relation'] not in ('train', 'resume'):
        raise ValueError('Multi-node execution requires managed ball training')
    primary = frozen['execution']
    profiles = [primary['profile']]
    if not primary['profile'].get('shared_storage'):
        raise ValueError('Multi-node training requires shared ReadWriteMany results storage')
    from .execution import freeze as node_freeze
    datasets = [service.get('dataset', d['id']) for d in frozen['datasets']]
    for id in ids[1:]:
        node = service.get('whale', id)
        execution = node_freeze(service, node, frozen['template'], datasets)
        p = execution['profile']
        if p['cluster_id'] != profiles[0]['cluster_id'] or p['namespace'] != profiles[0]['namespace']:
            raise ValueError('Distributed nodes must belong to one cluster and namespace')
        if any(p.get(k) != profiles[0].get(k) for k in ('image', 'claims', 'registry_path', 'mlflow_uri')):
            raise ValueError('Distributed nodes require identical images and shared storage')
        if not p.get('shared_storage') or p.get('max_gpus', 1) < gpu['count']:
            raise ValueError('Every node must enable this distributed GPU count and shared storage')
        if gpu['count'] > int(node['node'].get('allocatable', {}).get('nvidia.com/gpu', 0)):
            raise ValueError('A selected node has insufficient GPU capacity')
        if [l['root'] for l in execution['locations']] != [l['root'] for l in primary['locations']]:
            raise ValueError('Dataset mount paths must match across nodes')
        profiles.append(p)
    world = len(ids) * gpu['count']
    if frozen['config']['batch'] % world:
        raise ValueError('Global batch size must be divisible by total GPUs across all nodes')
    frozen['execution']['members'] = profiles
    frozen['execution']['rendezvous_timeout'] = 300


def commands(command):
    members = command['frozen']['execution'].get('members', [])
    if not members:
        return [command]
    result = []
    for rank, profile in enumerate(members):
        child = copy.deepcopy(command)
        child['frozen']['execution']['profile'] = profile
        child['frozen']['execution']['group'] = {'id': command['id'], 'rank': rank, 'size': len(members),
            'master': command['dispatch']['name']+'-master', 'timeout': command['frozen']['execution'].get('rendezvous_timeout', 300)}
        child['id'] = command['id']+'-n'+str(rank)
        child['result_id'] = command['id']
        child['dispatch'] = {'name': command['dispatch']['name']+'-n'+str(rank),
            'job_uid': command['dispatch'].get('member_uids', {}).get(str(rank))}
        result.append(child)
    return result


def service_manifest(command, owner):
    profile = command['frozen']['execution']['profile']
    return {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {
        'name': command['dispatch']['name']+'-master', 'namespace': profile['namespace'],
        'labels': {'geppetto.group-id': command['id']},
        'ownerReferences': [{'apiVersion': 'batch/v1', 'kind': 'Job', 'name': command['dispatch']['name']+'-n0', 'uid': owner}]},
        'spec': {'publishNotReadyAddresses': True, 'selector': {'geppetto.run-id': command['id']+'-n0'},
                 'ports': [{'name': 'rendezvous', 'port': 29400, 'targetPort': 29400}]}}
