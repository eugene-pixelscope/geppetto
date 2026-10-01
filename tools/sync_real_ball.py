"""Read-only import of verified operational runs; never submits training."""
import argparse
import csv
import io
import json
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from geppetto.adapters import MLflow, sync_mlflow
from geppetto.service import Service
from geppetto.storage import Store
from geppetto.workshop import reconcile

RUNS = {
 'de3450da71774bb4bb41c73c5d66d430': ('YOLO11x · 1920 · 기존 가중치 평가', '기존 학습 가중치 재평가. 기존 test 24,352장을 validation으로 재사용했으며 독립 테스트 점수가 아닙니다.'),
 '3bae118410ea4a88ac8be1acca5d88af': ('YOLO26x · 1920 · 100 epoch 모델 평가', '100 epoch 학습 완료 모델 재평가. 기존 test-as-val 분할이며 P2의 새 분할과 직접 비교할 수 없습니다.'),
 '7db09f122b5247b4b206224b4140ad12': ('YOLO26 P2 · 1920 · 체크포인트 복구 학습', '실제 Kubernetes 학습. recording-timestamp 분할: train 208,457 / val 23,187. 이전 실행 epoch 1 체크포인트에서 복구. 이 화면은 MLflow 조회 시점의 기록입니다.'),
 '8a83fa4e73284356b9aab35bb8f995e7': ('YOLO26x · one-to-many 절제 · 8 GPU', 'YOLO26x 구조와 DFL-free 회귀를 유지하고 one-to-one branch 및 dual-head loss를 제거한 인과 절제 학습. stage3-v4 test-as-validation, 1920px, 총 batch 32, GPU 8장.'),
 '1aac4017389f4d20a2864709534d68a2': ('YOLO26x · one-to-many 절제 · 8 GPU · RAM 수정 시도', 'Pod 한도를 256 GiB로 올렸지만 minikube 부모 cgroup의 48 GiB 제한이 남아 첫 배치에서 실패했습니다. 비교에서 제외합니다.'),
 '96d9d55987b0496abc67398ace190029': ('YOLO26x · one-to-many 절제 · 8 GPU · cgroup 수정', '유효한 재실행. Kubernetes Pod 한도 밖의 minikube 부모 cgroup 48 GiB 제한을 512 GiB로 수정했습니다. 모델·데이터·optimizer·batch 조건은 같습니다.'),
}
TRAINING_RUNS = {'7db09f122b5247b4b206224b4140ad12', '8a83fa4e73284356b9aab35bb8f995e7', '1aac4017389f4d20a2864709534d68a2', '96d9d55987b0496abc67398ace190029'}
ABLATION_ATTEMPTS = (
 '8a83fa4e73284356b9aab35bb8f995e7',
 '1aac4017389f4d20a2864709534d68a2',
 '96d9d55987b0496abc67398ace190029',
)
LIVE_RESULTS = {
 '96d9d55987b0496abc67398ace190029': {
  'namespace':'mlops',
  'airflow_run_id':'geppetto-ee3dc770-3687-4892-8181-4de3e7d073f3',
  'path':'/results/edad3fab2b054388acd221539558c957/train/results.csv',
 },
}

def live_metrics(spec, created_at):
    """Read completed epochs from the active DDP trainer without mutating it."""
    selector='run_id='+spec['airflow_run_id']
    pods=json.loads(subprocess.check_output(
        ['kubectl','get','pods','-n',spec['namespace'],'-l',selector,'-o','json'],
        text=True,timeout=10))['items']
    active=[p for p in pods if p.get('status',{}).get('phase')=='Running']
    if not active:return {},None
    name=max(active,key=lambda p:p['metadata'].get('creationTimestamp',''))['metadata']['name']
    raw=subprocess.check_output(
        ['kubectl','exec','-n',spec['namespace'],name,'-c','base','--','cat',spec['path']],
        text=True,timeout=10)
    metrics={}
    completed=None
    for row in csv.DictReader(io.StringIO(raw)):
        epoch=int(float(row['epoch']));completed=max(completed or epoch,epoch)
        elapsed=float(row.get('time') or 0)
        for key,value in row.items():
            if key in ('epoch','time') or value in (None,''):continue
            metrics.setdefault(key,[]).append({'step':epoch-1,'epoch':epoch,
                'timestamp':created_at+elapsed,'elapsed':elapsed,'value':float(value)})
    return metrics,completed

def sync(service, client):
    count=sync_mlflow(service,client,{'2':'ball-detection','5':'ball-detection','7':'ball-detection'},set(RUNS))
    reconcile(service.store)
    # All three jobs are attempts at the same ablation model.  Record the retry
    # lineage and canonical identity so they remain under one model on every API read.
    with service.store.connect() as db:
        available={r['id'] for r in service.store.all(db,'run')}
        dataset=next((d for d in service.store.all(db,'dataset') if d['name']=='Soccer Ball · Stage3 v4' and d.get('content_verified')),None)
        dataset_ref={'id':dataset['id'],'name':dataset['name'],'version':dataset['version']} if dataset else None
        attempts=[r for r in ABLATION_ATTEMPTS if 'mlflow-'+r in available]
        canonical=service.store.get(db,'run','mlflow-'+attempts[0])['pinocchio_id'] if attempts else None
        for index,run_id in enumerate(attempts):
            run=service.store.get(db,'run','mlflow-'+run_id)
            run['pinocchio_id']=canonical
            run['model_id']=canonical
            run['frozen']['relation']='train' if index==0 else 'resume'
            run['frozen']['parent_id']=None if index==0 else 'mlflow-'+attempts[index-1]
            service.store.put(db,'run',run)
    reconcile(service.store)
    with service.store.connect() as db:
        for run_id,(name,note) in RUNS.items():
            if 'mlflow-'+run_id not in available:continue
            run=service.store.get(db,'run','mlflow-'+run_id)
            run['name']=name
            run['provenance_note']=note
            run['source_kind']='training' if run_id in TRAINING_RUNS else 'evaluation'
            if dataset_ref:run['frozen']['datasets']=[dataset_ref]
            run['frozen']['evaluation_policy']=run.get('source_tags',{}).get('evaluation.split_policy')
            if run['source_kind']=='training':
                # This runner logs on_fit_epoch_end at zero-based trainer.epoch.
                points=[p for key,values in run['metrics'].items() if key.startswith(('train/','val/','metrics/')) for p in values]
                for point in points:point['epoch']=point['step']+1
                completed=max((p['epoch'] for p in points),default=None)
                run['progress']={'epoch':completed,'total':int(run['frozen']['config'].get('epochs',245)),
                                 'step':None,'epoch_basis':'completed'}
                if run['state']=='running' and run_id in LIVE_RESULTS and not points:
                    try:
                        live,completed=live_metrics(LIVE_RESULTS[run_id],run['created_at'])
                    except (OSError,subprocess.SubprocessError,ValueError,KeyError,json.JSONDecodeError):
                        live,completed={},None
                    if live:
                        run['metrics']=live
                        run['progress']['epoch']=completed
            if run_id=='3bae118410ea4a88ac8be1acca5d88af':
                # compare_mlflow.py logs the candidate CSV's explicit epoch as step.
                for key,points in run['metrics'].items():
                    if key.startswith('training_history/'):
                        for point in points:point['epoch']=point['step']
            run['logs']=[note]
            service.store.put(db,'run',run)
            pin=service.store.get(db,'pinocchio',run['pinocchio_id'])
            if run_id in ABLATION_ATTEMPTS and not pin.get('name_customized'):
                pin['name']='YOLO26x · one-to-many 절제'
            elif not pin.get('name_customized'):
                pin['name']=name.split(' · ')[0]
            service.store.put(db,'pinocchio',pin)
        used={r.get('pinocchio_id') for r in service.store.all(db,'run')}|{s.get('pinocchio_id') for s in service.store.all(db,'schedule')}
        for pin in service.store.all(db,'pinocchio'):
            if pin['id'] not in used:
                db.execute('DELETE FROM documents WHERE kind=? AND id=?',('pinocchio',pin['id']))
    return count

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home',type=Path,default=Path(__file__).resolve().parents[1]/'local/app')
    parser.add_argument('--url',default='http://127.0.0.1:5000')
    parser.add_argument('--watch',action='store_true',help='Refresh read-only observations every 60 seconds')
    args=parser.parse_args()
    service=Service(Store(args.home/'catalog.sqlite3'),mode='catalog')
    client=MLflow(args.url)
    try:
        while True:
            try:
                print(json.dumps({'imported':sync(service,client),'observed_at':time.time()}),flush=True)
            except Exception as exc:
                with service.store.connect() as db:
                    for run in service.store.all(db,'run'):
                        if run['id'] in {'mlflow-'+r for r in RUNS}:
                            run['fetch_error']=type(exc).__name__+': '+str(exc)
                            service.store.put(db,'run',run)
                if not args.watch:raise
                print(type(exc).__name__+': '+str(exc),file=sys.stderr,flush=True)
            if not args.watch:break
            time.sleep(60)
    finally:
        client.client.close()

if __name__=='__main__':main()
