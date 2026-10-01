"""Central registration and read-only inventory protocol. No remote job dispatch."""
import hashlib
import hmac
import secrets
import time
import uuid
from fastapi import HTTPException
from .configuration import digest


def hashed(value):return hashlib.sha256(value.encode()).hexdigest()
def text(value,limit=300):
    if not isinstance(value,str) or not value.strip() or len(value)>limit:raise ValueError('Invalid text field')
    return value.strip()

def public(c,now=None):
    now=time.time() if now is None else now
    result={k:v for k,v in c.items() if k not in ('bootstrap_hash','agent_hash','snapshot_digest')}
    if c.get('revoked'):state='revoked'
    elif not c.get('agent_hash'):state='awaiting_agent' if c['bootstrap_expires_at']>now else 'registration_expired'
    elif c.get('agent_expires_at',0)<=now:state='credential_expired'
    elif not c.get('approved'):state='pending_approval'
    elif not c.get('last_seen_at'):state='awaiting_snapshot'
    elif now-c['last_seen_at']>180:state='disconnected'
    elif now-c['last_seen_at']>60:state='stale'
    elif c.get('error'):state='degraded'
    else:state='connected'
    result['state']=state
    return result


def create(store,body):
    name=text(body.get('name'),160);token=secrets.token_urlsafe(32)
    item={'id':uuid.uuid4().hex,'name':name,'created_at':time.time(),'bootstrap_hash':hashed(token),
          'bootstrap_expires_at':time.time()+86400,'approved':False,'revoked':False,'sequence':-1,
          'nodes':[],'pvcs':[],'last_seen_at':None,'last_success_at':None,'error':None}
    with store.connect() as db:store.put(db,'cluster',item)
    return {**public(item),'bootstrap_token':token}


def rotate(store,id):
    token=secrets.token_urlsafe(32)
    with store.connect() as db:
        c=store.get(db,'cluster',id)
        c.update(bootstrap_hash=hashed(token),bootstrap_expires_at=time.time()+86400,approved=False,revoked=False,sequence=-1,
                 last_seen_at=None,error=None)
        c.pop('agent_hash',None);c.pop('snapshot_digest',None)
        store.put(db,'cluster',c)
    return {**public(c),'bootstrap_token':token}


def authorize(c,credential,bootstrap=False):
    key='bootstrap_hash' if bootstrap else 'agent_hash'
    expiry='bootstrap_expires_at' if bootstrap else 'agent_expires_at'
    if c.get('revoked') or not c.get(key) or c.get(expiry,0)<=time.time() or not hmac.compare_digest(c[key],hashed(credential)):
        raise HTTPException(401,'Invalid or expired agent credential')


def enroll(store,body,credential):
    cluster_uid=text(body.get('cluster_uid'));agent_id=text(body.get('agent_id'));token=text(body.get('agent_token'),128)
    if len(token)<40:raise ValueError('Agent token must contain at least 40 random characters')
    with store.connect() as db:
        c=store.get(db,'cluster',body.get('cluster_id',''))
        # A lost enrollment response can be retried by this exact identity until approval.
        if c.get('agent_hash'):
            authorize(c,token)
            if c.get('cluster_uid')!=cluster_uid or c.get('agent_id')!=agent_id:raise HTTPException(409,'Agent already enrolled')
            return public(c)
        authorize(c,credential,True)
        if c.get('cluster_uid') and c['cluster_uid']!=cluster_uid:raise HTTPException(409,'Cluster identity changed; create a new connection')
        for other in store.all(db,'cluster'):
            if other['id']!=c['id'] and other.get('cluster_uid')==cluster_uid and not other.get('revoked'):
                raise HTTPException(409,'Cluster is already registered')
        c.update(cluster_uid=cluster_uid,agent_id=agent_id,agent_hash=hashed(token),agent_expires_at=time.time()+365*86400,
                 enrolled_at=time.time(),version=text(body.get('version','unknown'),50))
        c.pop('bootstrap_hash',None)
        store.put(db,'cluster',c)
    return public(c)


def resources(value):
    if not isinstance(value,dict):raise ValueError('Invalid resource map')
    return {key:text(str(value[key]),60) for key in ('cpu','memory','nvidia.com/gpu','amd.com/gpu') if key in value}


def snapshot(store,body,credential):
    sequence=body.get('sequence')
    if type(sequence) is not int or sequence<0:raise ValueError('Invalid sequence')
    with store.connect() as db:
        c=store.get(db,'cluster',body.get('cluster_id',''));authorize(c,credential)
        if not c.get('approved'):raise HTTPException(403,'Agent requires central approval')
        if body.get('cluster_uid')!=c['cluster_uid'] or body.get('agent_id')!=c['agent_id']:raise HTTPException(409,'Agent identity mismatch')
        value=digest(body)
        if sequence<=c['sequence']:
            if sequence==c['sequence'] and value==c.get('snapshot_digest'):return {'accepted':True,'duplicate':True}
            raise HTTPException(409,'Out-of-order inventory')
        error=body.get('error')
        if error:
            c['error']=text(error,500)
        else:
            nodes=body.get('nodes');pvcs=body.get('pvcs',[])
            if not isinstance(nodes,list) or len(nodes)>5000 or not isinstance(pvcs,list) or len(pvcs)>10000:raise ValueError('Invalid inventory size')
            cleaned=[];seen=set()
            for node in nodes:
                if not isinstance(node,dict):raise ValueError('Invalid node')
                uid=text(node.get('uid'));name=text(node.get('name'))
                if uid in seen:raise ValueError('Duplicate node UID')
                seen.add(uid)
                if node.get('ready') not in ('True','False','Unknown') or type(node.get('unschedulable')) is not bool:raise ValueError('Invalid node condition')
                cleaned.append({'uid':uid,'name':name,'ready':node['ready'],'unschedulable':node['unschedulable'],
                    'capacity':resources(node.get('capacity',{})),'allocatable':resources(node.get('allocatable',{})),
                    'kubelet_version':text(node.get('kubelet_version','unknown'),80)})
            from .node_metrics import clean
            for original, target in zip(nodes, cleaned):
                if original.get('telemetry'):
                    target['telemetry'] = clean(original['telemetry'])
                if original.get('allocation'):
                    allocation = original['allocation']
                    from .analytics import finite
                    target['allocation'] = {k: finite(allocation[k]) for k in ('observed_at','cpu','memory','gpu','pods','managed_gpu') if k in allocation}
                    if any(v < 0 for v in target['allocation'].values()):
                        raise ValueError('Invalid node allocation')
                    mapping = allocation.get('run_gpus', {})
                    if not isinstance(mapping, dict) or len(mapping) > 10000: raise ValueError('Invalid workload allocation')
                    target['allocation']['run_gpus'] = {text(k,100): finite(v) for k,v in mapping.items() if k}
                    if any(v < 0 for v in target['allocation']['run_gpus'].values()): raise ValueError('Invalid workload allocation')
            if any(not isinstance(p,dict) for p in pvcs):raise ValueError('Invalid PVC')
            claims=[{'uid':text(p.get('uid')),'name':text(p.get('name')),'namespace':text(p.get('namespace')),
                     'phase':text(p.get('phase','Unknown'),60),'access_modes':[m for m in p.get('access_modes',[]) if m in ('ReadWriteOnce','ReadWriteMany','ReadOnlyMany','ReadWriteOncePod')]} for p in pvcs]
            c.update(nodes=cleaned,pvcs=claims,last_success_at=time.time(),error=None)
            for whale in store.all(db,'whale'):
                if whale.get('cluster_id')!=c['id']:continue
                node=next((n for n in cleaned if n['uid']==whale['node_uid']),None)
                whale.update(present=node is not None,observed_at=c['last_success_at'])
                if node:
                    whale.update(node=node,host=node['name'])
                    metric = node.get('telemetry')
                    if metric and metric.get('observed_at'):
                        history = whale.setdefault('resource_history', [])
                        if not history or metric['observed_at'] > history[-1]['observed_at']:
                            history.append(metric)
                        whale['resource_history'] = history[-240:]
                store.put(db,'whale',whale)
        c.update(sequence=sequence,snapshot_digest=value,last_seen_at=time.time())
        store.put(db,'cluster',c)
    return {'accepted':True}


def control(store,id,action):
    with store.connect() as db:
        c=store.get(db,'cluster',id)
        if action=='approve':
            if not c.get('agent_hash') or c.get('revoked'):raise ValueError('Enroll an agent first')
            c['approved']=True
        elif action=='revoke':c.update(revoked=True,approved=False)
        else:raise ValueError('Unknown cluster action')
        store.put(db,'cluster',c)
    return public(c)


def adopt(store,id,uid):
    with store.connect() as db:
        c=store.get(db,'cluster',id)
        if public(c)['state']!='connected' or time.time()-(c.get('last_success_at') or 0)>60:raise ValueError('A current successful inventory is required')
        node=next((n for n in c['nodes'] if n['uid']==uid),None)
        if not node:raise KeyError('Node not found')
        key='kube-'+digest([id,uid])[:24]
        existing=next((w for w in store.all(db,'whale') if w['id']==key),None)
        if existing:return existing
        whale={'id':key,'name':node['name'],'host':node['name'],'cluster_id':id,'node_uid':uid,'node':node,
               'connection':'kubernetes-agent','description':c['name']+' · Kubernetes node · Read-only agent',
               'present':True,'observed_at':c['last_success_at'],'template_ids':[],'created_at':time.time()}
        store.put(db,'whale',whale)
    return whale
