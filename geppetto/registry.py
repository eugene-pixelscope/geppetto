"""MLflow model aliases with explicit version review and durable audit records."""
import os
import re
import time
import uuid
import httpx
from .adapters import MLflow
from .configuration import digest


def client():
    url=os.environ.get('GEPPETTO_MLFLOW_URL')
    if not url:raise ValueError('MLflow registry is not configured')
    return MLflow(url,os.environ.get('GEPPETTO_MLFLOW_TOKEN'))


def versions(service):
    # Only versions attached to this catalog are eligible for changes.
    return [{**r['model_version'],'geppetto_run_id':r['id'],'state':r['state']} for r in service.list('run',limit=100000)['items'] if r.get('model_version')]


def preview(service,body):
    run=service.get('run',body['run_id']);version=run.get('model_version')
    if run['state']!='succeeded' or not version:raise ValueError('Select a successful registered model version')
    alias=body.get('alias','')
    if not re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,63}',alias) or re.fullmatch('v[0-9]+',alias):raise ValueError('Invalid model alias')
    remote=client()
    try:
        target=remote.call('model-versions/get',params={'name':version['name'],'version':version['version']})['model_version']
        if target.get('run_id')!=version['run_id'] or target.get('status')!='READY':raise ValueError('Registry version identity or readiness differs')
        model=remote.call('registered-models/get',params={'name':version['name']})['registered_model']
        previous=next((str(a['version']) for a in model.get('aliases',[]) if a['alias']==alias),None)
        spec={'run_id':run['id'],'name':version['name'],'version':str(version['version']),'alias':alias,'previous':previous}
        return {'spec':spec,'confirmation_hash':digest(spec)}
    finally:remote.client.close()


def promote(service,body):
    if service.mode=='readonly':raise ValueError('Read-only mode')
    value=preview(service,body)
    if value['confirmation_hash']!=body.get('confirmation_hash'):raise ValueError('Alias changed; review the current version again')
    spec=value['spec'];item={'id':uuid.uuid4().hex,**spec,'state':'pending','created_at':time.time()}
    with service.store.connect() as db:service.store.put(db,'registry_audit',item)
    remote=client()
    try:
        remote.call('registered-models/alias',{'name':spec['name'],'alias':spec['alias'],'version':spec['version']})
        observed=remote.call('registered-models/alias',params={'name':spec['name'],'alias':spec['alias']})['model_version']['version']
        if str(observed)!=spec['version']:raise ValueError('Alias changed concurrently in MLflow')
        item.update(state='confirmed',finished_at=time.time())
    except Exception as e:
        item.update(state='unknown',error=str(e)[:500]);raise
    finally:
        remote.client.close()
        with service.store.connect() as db:service.store.put(db,'registry_audit',item)
    return item
