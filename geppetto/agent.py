"""Outbound Kubernetes Agent; execution requires an explicit local allowlist."""
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import ssl
import time
from urllib.parse import urlparse
import uuid
import httpx

VERSION='0.4.0'


def validate_server(value):
    parsed=urlparse(value)
    if parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in ('127.0.0.1','localhost','::1')):
        raise ValueError('Central server must use HTTPS (loopback HTTP is allowed for local tests)')
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
        raise ValueError('Use a server origin without credentials, query or path')
    return value.rstrip('/')


def save(path,state):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(state,stream);stream.flush();os.fsync(stream.fileno())
    os.replace(tmp,path)


def identity(path,cluster_id,bootstrap):
    signature=hashlib.sha256(bootstrap.encode()).hexdigest()
    if path.exists():
        state=json.loads(path.read_text())
        if state['cluster_id']==cluster_id and state.get('bootstrap_signature')==signature:return state
    state={'cluster_id':cluster_id,'agent_id':uuid.uuid4().hex,'agent_token':secrets.token_urlsafe(48),
           'bootstrap_signature':signature,'enrolled':False,'sequence':0}
    save(path,state)
    return state


class KubernetesInventory:
    def __init__(self,client,token_file,namespace):
        self.client,self.token_file,self.namespace=client,Path(token_file),namespace

    def get(self,path,params=None):
        r=self.client.get(path,params=params,headers={'Authorization':'Bearer '+self.token_file.read_text().strip()})
        r.raise_for_status();return r.json()

    def pages(self,path):
        items=[];continuation='';seen=set()
        while True:
            value=self.get(path,{'limit':500,**({'continue':continuation} if continuation else {})})
            items.extend(value.get('items',[]))
            if len(items)>10000:raise ValueError('Inventory exceeds supported limit')
            continuation=value.get('metadata',{}).get('continue','')
            if not continuation:return items
            if continuation in seen:raise ValueError('Repeated pagination token')
            seen.add(continuation)

    def cluster_uid(self):return self.get('/api/v1/namespaces/kube-system')['metadata']['uid']

    def snapshot(self):
        nodes=[]
        for n in self.pages('/api/v1/nodes'):
            status=n.get('status',{})
            nodes.append({'uid':n['metadata']['uid'],'name':n['metadata']['name'],
                'ready':next((c['status'] for c in status.get('conditions',[]) if c['type']=='Ready'),'Unknown'),
                'unschedulable':bool(n.get('spec',{}).get('unschedulable',False)),
                'capacity':status.get('capacity',{}),'allocatable':status.get('allocatable',{}),
                'kubelet_version':status.get('nodeInfo',{}).get('kubeletVersion','unknown')})
        pvcs=[{'uid':p['metadata']['uid'],'name':p['metadata']['name'],'namespace':self.namespace,
               'phase':p.get('status',{}).get('phase','Unknown'),'access_modes':p.get('spec',{}).get('accessModes',[])}
              for p in self.pages('/api/v1/namespaces/'+self.namespace+'/persistentvolumeclaims')]
        from .node_metrics import collect_inventory
        collect_inventory(self, nodes)
        return {'nodes':nodes,'pvcs':pvcs}


def cycle(central,kube,state,path,bootstrap):
    if not state.get('cluster_uid'):state['cluster_uid']=kube.cluster_uid()
    headers={'X-Geppetto-Request':'1'}
    if not state['enrolled']:
        response=central.post('/api/agent/enroll',json={**state,'version':VERSION},
                              headers={**headers,'Authorization':'Bearer '+bootstrap})
        response.raise_for_status();state['enrolled']=True;save(path,state)
    state['sequence']+=1;save(path,state)
    payload={k:state[k] for k in ('cluster_id','cluster_uid','agent_id','sequence')}
    try:
        if kube.cluster_uid()!=state['cluster_uid']:raise ValueError('Kubernetes cluster identity changed')
        payload.update(kube.snapshot())
    except (httpx.HTTPError,OSError,ValueError,KeyError) as exc:
        payload['error']='Kubernetes inventory unavailable: '+type(exc).__name__
    response=central.post('/api/agent/snapshot',json=payload,
                          headers={**headers,'Authorization':'Bearer '+state['agent_token']})
    response.raise_for_status()
    return response.json()


def main():
    import signal
    def shutdown(signum,frame):raise SystemExit(0)
    signal.signal(signal.SIGTERM,shutdown)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    server=validate_server(os.environ['GEPPETTO_SERVER_URL'])
    cluster=os.environ['GEPPETTO_CLUSTER_ID'];bootstrap=os.environ['GEPPETTO_BOOTSTRAP_TOKEN']
    path=Path(os.environ.get('GEPPETTO_AGENT_STATE','/state/identity.json'))
    state=identity(path,cluster,bootstrap)
    sa=Path('/var/run/secrets/kubernetes.io/serviceaccount')
    namespace=os.environ.get('GEPPETTO_AGENT_NAMESPACE') or (sa/'namespace').read_text().strip()
    import re
    if not re.fullmatch('[a-z0-9]([-a-z0-9]*[a-z0-9])?',namespace):raise ValueError('Invalid namespace')
    central_ca=os.environ.get('GEPPETTO_SERVER_CA')
    with httpx.Client(base_url=server,timeout=30,verify=ssl.create_default_context(cafile=central_ca)) as central, \
         httpx.Client(base_url='https://kubernetes.default.svc',timeout=30,verify=ssl.create_default_context(cafile=str(sa/'ca.crt'))) as client:
        kube=KubernetesInventory(client,sa/'token',namespace)
        executor=None
        if os.environ.get('GEPPETTO_AGENT_EXECUTION_POLICY'):
            from .job_agent import JobAgent
            executor=JobAgent(kube,json.loads(Path(os.environ['GEPPETTO_AGENT_EXECUTION_POLICY']).read_text()),
                              os.environ.get('GEPPETTO_AGENT_RESULTS_ROOT'))
        while True:
            try:
                cycle(central,kube,state,path,bootstrap)
                if executor:
                    from .job_agent import cycle as job_cycle
                    job_cycle(central,executor,state,path)
                logging.info('Inventory delivered')
            except httpx.HTTPStatusError as exc:
                logging.warning('Connection rejected (HTTP %s); credentials are not logged',exc.response.status_code)
            except (httpx.HTTPError,OSError,ValueError,KeyError) as exc:
                logging.warning('Inventory cycle failed (%s)',type(exc).__name__)
            time.sleep(5 if executor else 20)


if __name__=='__main__':main()
