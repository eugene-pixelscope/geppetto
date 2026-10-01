"""Project-scoped SDK ingestion. Records only; never starts/stops training processes."""
import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import time
import uuid
from fastapi import HTTPException
from .configuration import digest
from .workshop import LOCAL_WHALE, new_pinocchio, reconcile


def text(value, limit=160):
    if not isinstance(value,str) or not value.strip() or len(value)>limit:raise ValueError('Invalid text')
    return value.strip()


def issue(service,body):
    if service.mode=='readonly':raise ValueError('Read-only mode')
    reconcile(service.store)
    project=service.get('project',body.get('project_id',''))
    token=secrets.token_urlsafe(48)
    item={'id':uuid.uuid4().hex,'name':text(body.get('name','Training SDK')),'project_id':project['id'],
          'token_hash':hashlib.sha256(token.encode()).hexdigest(),'created_at':time.time(),
          'expires_at':time.time()+30*86400,'revoked':False}
    with service.store.connect() as db:service.store.put(db,'sdk_token',item)
    return {**{k:v for k,v in item.items() if k!='token_hash'},'token':token}


def authorize(store,db,credential):
    hashed=hashlib.sha256(credential.encode()).hexdigest()
    for token in store.all(db,'sdk_token'):
        if hmac.compare_digest(token['token_hash'],hashed) and not token['revoked'] and token['expires_at']>time.time():return token
    raise HTTPException(401,'Invalid or expired SDK token')


def owned(store,db,id,credential):
    token=authorize(store,db,credential);run=store.get(db,'run',id)
    if run.get('tracking')!='sdk' or run.get('project_id')!=token['project_id']:
        raise HTTPException(403,'SDK token cannot access this run')
    return run


def request_key(body):
    key=body.get('request_id')
    if not isinstance(key,str) or len(key)!=32 or any(c not in '0123456789abcdef' for c in key):raise ValueError('request_id must be UUID hex')
    return key


def init(service,body,credential):
    if service.mode=='readonly':raise ValueError('Read-only mode')
    key=request_key(body)
    with service.store.connect() as db:
        token=authorize(service.store,db,credential)
        project=service.store.get(db,'project',token['project_id'])
        if body.get('project') not in (None,project['id'],project['name']):raise HTTPException(403,'Project does not match SDK token')
        id='sdk-'+key
        previous=next((r for r in service.store.all(db,'run') if r['id']==id),None)
        if previous:
            if previous.get('project_id')!=project['id'] or previous.get('init_digest')!=digest(body):raise ValueError('Conflicting initialization')
            return response(previous)
        relation=body.get('job_type','train')
        if relation not in ('train','resume','evaluate'):raise ValueError('Invalid job_type')
        parent=None
        if relation!='train':parent=owned(service.store,db,body.get('parent_run_id',''),credential)
        if relation=='resume' and any(r.get('pinocchio_id')==parent['pinocchio_id'] and r['state']=='running' and r.get('source_kind')!='evaluation' for r in service.store.all(db,'run')):raise ValueError('This model already has an active training run')
        if relation=='resume' and parent['state'] not in ('failed','stopped'):raise ValueError('Finish the interrupted run as failed or stopped before resuming')
        config=body.get('config',parent['frozen']['config'] if parent else {})
        if not isinstance(config,dict) or len(json.dumps(config,allow_nan=False))>65536:raise ValueError('config must be a JSON object up to 64 KiB')
        if relation=='resume' and config!=parent['frozen']['config']:raise ValueError('Resume must preserve configuration')
        node_id=body.get('node_id') or (parent['node_id'] if parent else LOCAL_WHALE)
        if parent and node_id!=parent['node_id']:raise ValueError('Related runs must use the same environment')
        node=service.store.get(db,'whale',node_id)
        datasets=[]
        if body.get('dataset_id'):
            dataset=service.store.get(db,'dataset',body['dataset_id'])
            if dataset['category']!=project['category']:raise ValueError('Dataset category differs from project')
            datasets=[{'id':dataset['id'],'version':dataset['version']}]
        elif parent:datasets=parent['frozen']['datasets']
        if relation=='resume' and datasets!=parent['frozen']['datasets']:raise ValueError('Resume must preserve dataset version')
        architecture=text(body.get('architecture') or (parent.get('architecture') if parent else 'Custom model'))
        name=text(body.get('name') or architecture)
        if parent:
            pid=parent['pinocchio_id']
            if architecture!=parent.get('architecture'):raise ValueError('Parent architecture differs')
        else:
            pin=new_pinocchio(service.store,db,node['id'],{'id':None,'category':project['category'],'name':name})
            pin['project_id']=project['id'];service.store.put(db,'pinocchio',pin);pid=pin['id']
        now=time.time()
        run={'id':id,'name':name,'category':project['category'],'project_id':project['id'],'node_id':node['id'],
             'pinocchio_id':pid,'model_id':pid,'template_id':None,'architecture':architecture,
             'tracking':'sdk','imported':True,'synthetic':False,'source_kind':'evaluation' if relation=='evaluate' else 'training',
             'created_at':now,'state':'running','phase':'validation' if relation=='evaluate' else 'training',
             'metrics':{},'logs':[],'artifacts':[],'airflow':[],'mlflow':[],'progress':{'epoch':None,'total':None,'step':None},
             'frozen':{'config':config,'config_sha256':digest(config),'datasets':datasets,'evaluation_policy':body.get('evaluation_policy'),
                       'relation':relation,'parent_id':parent['id'] if parent else None},
             'last_success_at':now,'observed_at':now,'fetch_error':None,'stop_requested_at':None,
             'init_digest':digest(body),'sdk_last_step':-1}
        service.store.put(db,'run',run)
        return response(run)


def response(run):return {**{k:run[k] for k in ('id','model_id','project_id','state')},'last_step':run.get('sdk_last_step',-1)}


def event(service,id,body,credential):
    if service.mode=='readonly':raise ValueError('Read-only mode')
    key=request_key(body);kind=body.get('kind')
    with service.store.connect() as db:
        run=owned(service.store,db,id,credential)
        existing=db.execute('SELECT digest FROM requests WHERE key=?',('sdk:'+id+':'+key,)).fetchone()
        if existing:
            if existing[0]!=digest(body):raise ValueError('Conflicting retried event')
            return response(run)
        if run['state']!='running':raise ValueError('Run is already finished')
        now=time.time()
        if kind=='log':
            step=body.get('step');metrics=body.get('metrics',{})
            if type(step) is not int or step<run['sdk_last_step'] or step<0:raise ValueError('step must be non-negative and monotonic')
            if not isinstance(metrics,dict) or not 1<=len(metrics)<=100:raise ValueError('Provide 1–100 numeric metrics')
            if len(set(run['metrics'])|set(metrics))>200:raise ValueError('At most 200 metric keys per run')
            epoch=body.get('epoch')
            if epoch is not None and (type(epoch) is not int or epoch<0):raise ValueError('Invalid epoch')
            if epoch is not None and run['progress'].get('epoch') is not None and epoch<run['progress']['epoch']:raise ValueError('Epoch cannot move backwards')
            for name,value in metrics.items():
                text(name,160)
                if type(value) not in (int,float) or not math.isfinite(value):raise ValueError('Metrics must be finite numbers')
                values=run['metrics'].setdefault(name,[])
                if len(values)>=100000:raise ValueError('Metric history limit reached')
                point={'value':value,'step':step,'epoch':epoch,'timestamp':now,'elapsed':now-run['created_at']}
                if values and values[-1]['step']==step:
                    if values[-1]['value']!=value or values[-1]['epoch']!=epoch:raise ValueError('Metric at this step is immutable')
                else:values.append(point)
            run['sdk_last_step']=step;run['progress']['step']=step
            if epoch is not None:run['progress']['epoch']=epoch
        elif kind=='progress':
            epoch,total=body.get('epoch'),body.get('total_epochs');phase=body.get('phase','training')
            if type(epoch) is not int or type(total) is not int or not 0<=epoch<=total or total<=0 or phase not in ('training','validation'):
                raise ValueError('Invalid epoch progress')
            if run['progress'].get('epoch') is not None and epoch<run['progress']['epoch']:raise ValueError('Epoch cannot move backwards')
            run['progress'].update(epoch=epoch,total=total,phase=phase);run['phase']=phase
        elif kind=='heartbeat':pass
        elif kind=='finish':
            state=body.get('state','succeeded')
            if state not in ('succeeded','failed','stopped'):raise ValueError('Invalid final state')
            run.update(state=state,phase=state,finished_at=now)
            if body.get('message'):run['logs'].append(text(body['message'],2000))
        else:raise ValueError('Unknown event kind')
        run.update(last_success_at=now,observed_at=now,fetch_error=None)
        service.store.put(db,'run',run)
        db.execute('INSERT INTO requests VALUES(?,?,?,?)',('sdk:'+id+':'+key,digest(body),'sdk_event',id))
        return response(run)


def artifact(service,home,id,body,credential):
    if service.mode=='readonly':raise ValueError('Read-only mode')
    key=request_key(body);action=body.get('action');root=Path(home)/'sdk-artifacts'/id
    with service.store.connect() as db:
        run=owned(service.store,db,id,credential)
        upload_id=id+':'+key
        if action=='start':
            name=text(body.get('name'),200)
            if Path(name).name!=name or '/' in name or '\\' in name or name in ('.','..'):raise ValueError('Artifact name must be a filename')
            size=body.get('size');sha=body.get('sha256');role=body.get('role','checkpoint')
            if type(size) is not int or not 0<=size<=4*1024**3:raise ValueError('Artifact limit is 4 GiB')
            if not isinstance(sha,str) or len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):raise ValueError('Invalid SHA-256')
            if role not in ('checkpoint','inference_weights','evaluation','configuration'):raise ValueError('Invalid artifact role')
            old=next((u for u in service.store.all(db,'sdk_upload') if u['id']==upload_id),None)
            if old:
                if any(old[k]!=v for k,v in [('name',name),('size',size),('sha256',sha),('role',role)]):raise ValueError('Conflicting upload')
                return {'offset':old['offset'],'complete':old['complete']}
            if run['state']!='running':raise ValueError('Run is already finished')
            if sum(u['size'] for u in service.store.all(db,'sdk_upload'))+size>int(os.environ.get('GEPPETTO_SDK_QUOTA_BYTES',1024**3)):
                raise ValueError('SDK artifact storage quota exceeded')
            root.mkdir(parents=True,exist_ok=True);path=key+'-'+name
            # An orphan from a rolled-back start can safely be reset under this UUID.
            (root/path).write_bytes(b'')
            upload={'id':upload_id,'run_id':id,'name':name,'path':path,'size':size,'sha256':sha,'role':role,'offset':0,'complete':False}
            service.store.put(db,'sdk_upload',upload);return {'offset':0,'complete':False}
        upload=service.store.get(db,'sdk_upload',upload_id);path=root/upload['path']
        if action=='abort':
            if upload['complete']:raise ValueError('Published artifacts cannot be aborted')
            path.unlink(missing_ok=True)
            db.execute('DELETE FROM documents WHERE kind=? AND id=?',('sdk_upload',upload_id))
            return {'aborted':True}
        if action=='chunk':
            if upload['complete']:raise ValueError('Upload is complete')
            if run['state']!='running':raise ValueError('Run is already finished')
            try:data=base64.b64decode(body.get('data',''),validate=True)
            except (ValueError,TypeError):raise ValueError('Invalid base64')
            offset=body.get('offset')
            if type(offset) is not int or offset<0 or not 1<=len(data)<=1024**2 or offset+len(data)>upload['size']:raise ValueError('Invalid chunk')
            if offset<upload['offset']:
                with path.open('rb') as stream:stream.seek(offset);existing=stream.read(len(data))
                if offset+len(data)>upload['offset'] or existing!=data:raise ValueError('Conflicting retried chunk')
            else:
                if offset!=upload['offset']:raise ValueError('Out-of-order chunk')
                with path.open('r+b') as stream:
                    stream.seek(offset);stream.write(data);stream.truncate(offset+len(data));stream.flush();os.fsync(stream.fileno())
                upload['offset']+=len(data);service.store.put(db,'sdk_upload',upload)
            return {'offset':upload['offset']}
        if action!='complete':raise ValueError('Unknown artifact action')
        if upload['complete']:return {'complete':True,'sha256':upload['sha256']}
        if run['state']!='running':raise ValueError('Run is already finished')
        if upload['offset']!=upload['size']:raise ValueError('Upload is incomplete')
        with path.open('rb') as stream:sha=hashlib.file_digest(stream,'sha256').hexdigest()
        if sha!=upload['sha256']:raise ValueError('Artifact hash mismatch')
        run['artifacts'].append({'id':key,'path':upload['path'],'root':str(root),'size':upload['size'],'sha256':sha,
                                 'role':upload['role'],'complete':True,'resume':False,'remote':False})
        upload['complete']=True;service.store.put(db,'sdk_upload',upload);service.store.put(db,'run',run)
        return {'complete':True,'sha256':sha}
