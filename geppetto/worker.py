"""Dedicated-DAG outbox/reconciliation. Never targets legacy DAGs or imported runs."""
import os
import time
import httpx
import json
from pathlib import Path
from .adapters import Airflow,Kubernetes
from .files import verify_manifest
from .service import TERMINAL


def tick_live(service):
    url=os.environ.get('GEPPETTO_AIRFLOW_URL')
    token=os.environ.get('GEPPETTO_AIRFLOW_TOKEN')
    dag=os.environ.get('GEPPETTO_AIRFLOW_DAG')
    if not url or not token or not dag or not dag.startswith('geppetto_'):
        raise ValueError('Explicit dedicated geppetto_ DAG and credentials required')
    airflow=Airflow(url,token,dag)
    kube=None
    if os.environ.get('GEPPETTO_KUBE_URL'):
        kube=Kubernetes(os.environ['GEPPETTO_KUBE_URL'],os.environ['GEPPETTO_KUBE_TOKEN'],
                        os.environ['GEPPETTO_KUBE_NAMESPACE'],verify=os.environ.get('GEPPETTO_KUBE_CA',True))
    try:
        with service.store.connect() as db:
            service.tick_schedules(db,time.time())
            runs=sorted(service.store.all(db,'run'),key=lambda r:r['created_at'])
            active=sum(r['state'] not in TERMINAL and bool(r['airflow']) and not r.get('imported') for r in runs)
            for run in runs:
                if run.get('frozen',{}).get('execution',{}).get('backend')=='agent':
                    continue
                if run.get('imported') or run.get('synthetic') or (run['state'] in TERMINAL and not (run['stop_requested_at'] and run['airflow'])):
                    continue
                try:
                    id='geppetto-'+run['id']
                    if run['stop_requested_at']:
                        if not run['airflow'] and run['state']=='queued':
                            # A crash can occur after POST succeeds but before SQLite commits.
                            try:
                                remote=airflow.status(id)
                            except httpx.HTTPStatusError as exc:
                                if exc.response.status_code!=404:
                                    raise
                                run.update({'state':'stopped','resource_released':True})
                            else:
                                run['airflow']=[{'dag_id':dag,'run_id':id,'state':remote['state']}]
                                if kube is None:
                                    raise ValueError('Resource return verification is required')
                                airflow.stop(id)
                                if kube.stop(run['id']):
                                    run.update({'state':'stopped','resource_released':True})
                        else:
                            if kube is None:
                                raise ValueError('Cannot acknowledge stop without resource-return verification')
                            airflow.stop(id)
                            if kube.stop(run['id']):
                                run.update({'state':'stopped','resource_released':True})
                    elif not run['airflow']:
                        from .sweeps import can_dispatch
                        if not can_dispatch(service.store,db,run,runs):
                            continue
                        if active>=service.concurrency:
                            continue
                        try:
                            for dataset in run['frozen']['datasets']:
                                verify_manifest(service.store.get(db,'dataset',dataset['id']))
                        except (ValueError,OSError) as exc:
                            run.update({'state':'failed','phase':'Data validation failed','finished_at':time.time()})
                            run['logs'].append(str(exc))
                            run['fetch_error']=None
                            run['observed_at']=time.time()
                            service.store.put(db,'run',run)
                            continue
                        remote=airflow.submit(run)
                        run['airflow']=[{'dag_id':dag,'run_id':id,'state':remote['state']}]
                        run['state']='submitting'
                        active+=1
                    else:
                        remote=airflow.status(id)
                        run['airflow'][0]['state']=remote['state']
                        run['state']={'success':'succeeded','failed':'failed','running':'running','queued':'submitting'}.get(remote['state'],'unknown')
                        run['logs']=airflow.logs(id).splitlines()[-2000:]
                    collect_results(run)
                    run['last_success_at']=time.time()
                    run['fetch_error']=None
                except Exception as exc:
                    run['fetch_error']=type(exc).__name__+': '+str(exc)
                run['observed_at']=time.time()
                service.store.put(db,'run',run)
    finally:
        airflow.client.close()
        if kube:
            kube.client.close()


def collect_results(run):
    """Read the new project's dedicated, read-only results mount only."""
    base=os.environ.get('GEPPETTO_RESULTS_ROOT')
    if not base:
        return
    root=(Path(base).resolve()/run['id']).resolve()
    if not root.is_relative_to(Path(base).resolve()):
        raise ValueError('Invalid results path')
    for name in ('tracking.json','progress.json','result.json'):
        path=root/name
        if not path.is_file() or path.is_symlink():
            continue
        data=json.loads(path.read_text())
        if name!='tracking.json' and data.get('submission_id')!=run['id']:
            raise ValueError('Result belongs to a different execution')
        if name=='tracking.json':
            run['mlflow']=[{'run_id':data['mlflow_run_id'],'experiment_id':data['experiment_id']}]
        elif name=='progress.json':
            run['metrics']=data['metrics']
            run['progress']={'epoch':data.get('epoch'),'step':data.get('step'),'total':None}
            run['phase']=data.get('phase','unknown')
        elif run['state'] in TERMINAL:
            if run['state']=='succeeded' and data['state']!='succeeded':
                run['state']='failed'
            run['metrics']=data['metrics']
            run['artifacts']=[{**a,'root':str(root)} for a in data['artifacts']]
            run['model_version']=data.get('model_version')
            run['finished_at']=data['finished_at']
