"""Explicitly configured REST adapters. Instantiation never discovers credentials."""
from urllib.parse import quote
import time

import httpx


class MLflow:
    def __init__(self, url, token=None, transport=None):
        self.client = httpx.Client(base_url=url.rstrip('/'),headers={'Authorization':f'Bearer {token}'} if token else {},
                                   timeout=15,transport=transport)

    def call(self, endpoint, body=None, params=None):
        response = self.client.request('POST' if body is not None else 'GET','/api/2.0/mlflow/'+endpoint,
                                       json=body,params=params)
        response.raise_for_status()
        return response.json()

    def pages(self, endpoint, field, body):
        token = None
        while True:
            result = self.call(endpoint,{**body,**({'page_token':token} if token else {})})
            yield from result.get(field,[])
            next_token = result.get('next_page_token')
            if not next_token:
                break
            if next_token == token:
                raise ValueError('MLflow pagination did not advance')
            token = next_token

    def history(self, run_id, key):
        token, values = None, []
        while True:
            result = self.call('metrics/get-history',params={'run_id':run_id,'metric_key':key,'max_results':5000,
                                                           **({'page_token':token} if token else {})})
            values.extend(result.get('metrics',[]))
            next_token = result.get('next_page_token')
            if not next_token:
                return sorted(values,key=lambda v:(v['step'],v['timestamp']))
            if token == next_token:
                raise ValueError('MLflow pagination did not advance')
            token = next_token

    def artifacts(self, run_id, path=''):
        token=None
        while True:
            result=self.call('artifacts/list',params={'run_id':run_id,'path':path,**({'page_token':token} if token else {})})
            for file in result.get('files',[]):
                if file.get('is_dir'):
                    yield from self.artifacts(run_id,file['path'])
                else:
                    yield file
            next_token=result.get('next_page_token')
            if not next_token:
                break
            if token==next_token:
                raise ValueError('Artifact pagination did not advance')
            token=next_token


class Airflow:
    def __init__(self,url,token,dag_id,transport=None):
        self.dag_id = dag_id
        self.client = httpx.Client(base_url=url.rstrip('/'),headers={'Authorization':f'Bearer {token}'},timeout=20,transport=transport)

    def path(self,run_id=''):
        return '/api/v2/dags/'+quote(self.dag_id,safe='')+'/dagRuns'+('/'+quote(run_id,safe='') if run_id else '')

    def submit(self, run):
        id = 'geppetto-'+run['id']
        response = self.client.post(self.path(),json={'dag_run_id':id,'logical_date':None,'conf':{
            'submission_id':run['id'],'frozen':run['frozen'],'display_name':run['name']}})
        if response.status_code == 409:
            return self.status(id)
        response.raise_for_status()
        return response.json()

    def status(self,id):
        response = self.client.get(self.path(id))
        response.raise_for_status()
        return response.json()

    def stop(self,id):
        response = self.client.patch(self.path(id),json={'state':'failed','note':'Geppetto stop requested'})
        response.raise_for_status()

    def logs(self,id,task='train',attempt=1):
        response = self.client.get(self.path(id)+'/taskInstances/'+quote(task,safe='')+'/logs/'+str(attempt))
        response.raise_for_status()
        return response.text[-262144:]


class Kubernetes:
    def __init__(self,url,token,namespace,verify=True,transport=None):
        self.namespace=namespace
        self.client=httpx.Client(base_url=url.rstrip('/'),headers={'Authorization':f'Bearer {token}'},verify=verify,timeout=15,transport=transport)

    def matching(self,submission):
        response=self.client.get('/api/v1/namespaces/'+quote(self.namespace,safe='')+'/pods',
                                 params={'labelSelector':'geppetto-submission='+submission})
        response.raise_for_status()
        return [p for p in response.json()['items'] if p['metadata'].get('labels',{}).get('geppetto-submission')==submission]

    def stop(self,submission):
        for pod in self.matching(submission):
            meta=pod['metadata']
            response=self.client.request('DELETE','/api/v1/namespaces/'+quote(self.namespace,safe='')+'/pods/'+quote(meta['name'],safe=''),
                json={'apiVersion':'v1','kind':'DeleteOptions','gracePeriodSeconds':30,'preconditions':{'uid':meta['uid']}})
            if response.status_code not in (404,):
                response.raise_for_status()
        return not self.matching(submission)


def sync_mlflow(service, client, experiment_map, run_ids=None):
    """Import only explicitly mapped experiment IDs. Unknown metadata stays unknown."""
    count=0
    for experiment_id, category in experiment_map.items():
        for raw in client.pages('runs/search','runs',{'experiment_ids':[experiment_id],'max_results':100}):
            info,data=raw['info'],raw['data']
            if run_ids is not None and info['run_id'] not in run_ids:
                continue
            with service.store.connect() as db:
                if db.execute("SELECT 1 FROM documents WHERE kind='deleted_run' AND id=?",('mlflow-'+info['run_id'],)).fetchone():
                    continue
            tags={x['key']:x['value'] for x in data.get('tags',[])}
            id='mlflow-'+info['run_id']
            metrics={}
            for metric in data.get('metrics',[]):
                history=client.history(info['run_id'],metric['key'])
                metrics[metric['key']]=[{'step':p['step'],'epoch':None,'timestamp':p['timestamp']/1000,
                                       'elapsed':(p['timestamp']-info['start_time'])/1000,'value':p['value']} for p in history]
            artifacts=[]
            if info['status'] in ('FINISHED','FAILED','KILLED'):
                for index,file in enumerate(client.artifacts(info['run_id'])):
                    path=file['path']
                    role='evaluation_image' if path.lower().endswith(('.png','.jpg','.jpeg')) else 'inference_weights' if path.endswith(('.pt','.pth','.safetensors')) else 'configuration' if path.endswith(('.yaml','.yml')) else 'evaluation'
                    artifacts.append({'id':str(index),'path':path,'size':file.get('file_size'),
                                      'role':role,'complete':True,'resume':False,'remote':True,
                                      'artifact_uri':info.get('artifact_uri'),'run_id':info['run_id']})
            item={'id':id,'name':tags.get('mlflow.runName',info['run_id']),'category':category,
                  'template_id':None,'imported':True,'synthetic':False,'created_at':info['start_time']/1000,
                  'state':{'FINISHED':'succeeded','FAILED':'failed','KILLED':'stopped','RUNNING':'running'}.get(info['status'],'unknown'),
                  'phase':info['status'],'metrics':metrics,'artifacts':artifacts,'logs':[],
                  'frozen':{'config':{p['key']:p['value'] for p in data.get('params',[])},'datasets':None,'evaluation_policy':None},
                  'airflow':[], 'mlflow':[{'run_id':info['run_id'],'experiment_id':experiment_id,'state':info['status']}],
                  'progress':{'epoch':None,'step':None,'total':None},'stop_requested_at':None,
                  'source_tags':tags, 'source_kind':tags.get('run_type','external'),
                  'observed_at':time.time(),'last_success_at':time.time(),'fetch_error':None}
            with service.store.connect() as db:
                if db.execute("SELECT 1 FROM documents WHERE kind='deleted_run' AND id=?",(id,)).fetchone():
                    continue
                try:
                    previous=service.store.get(db,'run',id)
                except KeyError:
                    previous={}
                # A later MLflow response may omit metric metadata for a failed or
                # archived run. Keep histories already observed in the catalog.
                if not metrics and previous.get('metrics'):
                    item['metrics'] = previous['metrics']
                for key in ('metric_recovery',):
                    if key in previous:item[key]=previous[key]
                for key in ('node_id','pinocchio_id','provenance_note','display_name','model_id','project_id'):
                    if key in previous:item[key]=previous[key]
                for key in ('relation','parent_id','datasets'):
                    if key in previous.get('frozen',{}):
                        item['frozen'][key]=previous['frozen'][key]
                service.store.put(db,'run',item)
            count+=1
    return count
