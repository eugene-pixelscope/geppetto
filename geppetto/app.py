from contextlib import asynccontextmanager
import asyncio
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import tempfile
import time
from pathlib import PurePosixPath
from urllib.parse import urlparse,quote

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jsonschema.exceptions import ValidationError

from .catalog import CATEGORIES, ROOT
from .demo import seed, tick
from .files import open_beneath
from .indexed_datasets import sample_page,sample_at
from .service import Service
from .storage import Store


def create_app(home=None, mode=None, start_worker=True, token=None, auth_required=True):
    home = Path(home or os.environ.get('GEPPETTO_HOME', ROOT/'local/app')).resolve()
    mode = mode or os.environ.get('GEPPETTO_MODE','catalog')
    token = token if token is not None else os.environ.get('GEPPETTO_TOKEN','')
    roots = [str(home/'demo-data')] + [p for p in os.environ.get('GEPPETTO_DATA_ROOTS','').split(os.pathsep) if p]
    store=Store(home/'catalog.sqlite3')
    with store.connect() as db:
        clean=any(v.get('demo_disabled') for v in store.all(db,'workspace'))
    if clean and mode=='demo':mode='catalog'
    service = Service(store,mode,roots)
    if mode == 'demo':
        seed(service,home)
    from .node_metrics import HostCollector
    local_metrics = HostCollector('/proc', '/') if os.environ.get('GEPPETTO_LOCAL_METRICS') == '1' else None
    async def worker():
        while True:
            try:
                if mode!='readonly':
                    from .dataset_versions import tick as verify_datasets
                    await asyncio.to_thread(verify_datasets,service,home)
                    from .pipelines import tick_registry
                    await asyncio.to_thread(tick_registry,service)
                if mode == 'demo':
                    await asyncio.to_thread(tick,service,home)
                elif mode == 'live':
                    from .worker import tick_live
                    await asyncio.to_thread(tick_live,service)
                if local_metrics and mode != 'readonly':
                    from .node_metrics import sample_local
                    await asyncio.to_thread(sample_local,service,local_metrics)
                app.state.worker_error=None
            except Exception as exc:
                app.state.worker_error=type(exc).__name__+': '+str(exc)
            await asyncio.sleep(3)
    @asynccontextmanager
    async def lifespan(app):
        task=asyncio.create_task(worker()) if start_worker else None
        yield
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    app=FastAPI(title='Geppetto',version='0.4.0',lifespan=lifespan,docs_url=None,redoc_url=None)
    app.state.service=service
    app.state.worker_error=None
    from .auth import Accounts, SESSION_SECONDS
    accounts = Accounts(store, home)
    app.state.accounts = accounts
    hosts=set(os.environ.get('GEPPETTO_ALLOWED_HOSTS','127.0.0.1,localhost,testserver').split(','))

    @app.middleware('http')
    async def boundary(request,call_next):
        hostname=urlparse('http://'+request.headers.get('host','')).hostname
        if hostname not in hosts:
            return JSONResponse({'detail':'Host is not allowed'},status_code=403)
        if request.method not in ('GET','HEAD','OPTIONS'):
            origin=request.headers.get('origin')
            if request.headers.get('x-geppetto-request') != '1' or (origin and origin != str(request.base_url).rstrip('/')):
                return JSONResponse({'detail':'Invalid request origin'},status_code=403)
            if not request.headers.get('content-type','').startswith('application/json'):
                return JSONResponse({'detail':'JSON is required'},status_code=415)
            if int(request.headers.get('content-length','0')) > 16*1024*1024:
                return JSONResponse({'detail':'Request too large'},status_code=413)
        path = request.url.path
        public_paths = ('/api/login', '/api/health', '/api/auth/status', '/api/auth/setup')
        machine_path = path in ('/api/agent/enroll','/api/agent/snapshot','/api/agent/jobs/poll','/api/agent/jobs/observe') or path.startswith('/api/tracking/')
        request.state.user = None
        bearer = request.headers.get('authorization', '').removeprefix('Bearer ')
        machine_admin = bool(token and hmac.compare_digest(bearer.encode(), token.encode()))
        if path.startswith('/api/') and path not in public_paths and not machine_path:
            user = await asyncio.to_thread(accounts.resolve, request.cookies.get('geppetto_session', ''))
            configured = await asyncio.to_thread(accounts.configured)
            if machine_admin:
                user = {'id': 'service-token', 'username': 'service-token', 'role': 'admin', 'must_change_password': False}
            if not user and (token or configured or auth_required):
                return JSONResponse({'detail': 'Sign in required.'}, status_code=401)
            request.state.user = user or {'id':'local-development','username':'local-development','role':'admin','must_change_password':False}
            if user and user['must_change_password'] and path not in ('/api/auth/me','/api/auth/password','/api/logout'):
                return JSONResponse({'detail': 'Change your password first.', 'code': 'password_change_required'}, status_code=403)
            admin_only = path.startswith(('/api/auth/users', '/api/auth/audit', '/api/sdk/tokens')) or (
                request.method not in ('GET','HEAD','OPTIONS') and (
                    path.startswith(('/api/clusters', '/api/integrations/')) or path == '/api/whales' or
                    path.endswith(('/execution', '/locations'))))
            if admin_only and request.state.user['role'] != 'admin':
                return JSONResponse({'detail':'Administrator access required.'}, status_code=403)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control']='no-store'
        return response

    @app.exception_handler(ValueError)
    @app.exception_handler(ValidationError)
    async def invalid(request,exc):
        return JSONResponse({'detail':str(exc)},status_code=422)

    @app.exception_handler(KeyError)
    async def missing(request,exc):
        return JSONResponse({'detail':str(exc)},status_code=404)

    @app.get('/api/health')
    def health():
        return {'status':'ok','version':app.version}

    def session_response(result):
        user, secret = result
        response = JSONResponse({'user': user})
        response.set_cookie('geppetto_session', secret, httponly=True, samesite='strict',
                            secure=os.environ.get('GEPPETTO_SECURE_COOKIE')=='1', max_age=SESSION_SECONDS)
        return response

    @app.get('/api/auth/status')
    def auth_status():
        return {'setup_required': not accounts.configured()}

    @app.post('/api/auth/setup')
    def setup_account(body:dict, request:Request):
        return session_response(accounts.setup(body, request.client.host if request.client else 'unknown'))

    @app.post('/api/login')
    def login(body:dict, request:Request):
        return session_response(accounts.login(body, request.client.host if request.client else 'unknown'))

    @app.post('/api/logout')
    def logout(request:Request, body:dict):
        accounts.logout(request.cookies.get('geppetto_session', ''))
        response = JSONResponse({'ok':True})
        response.delete_cookie('geppetto_session', httponly=True, samesite='strict',
                               secure=os.environ.get('GEPPETTO_SECURE_COOKIE')=='1')
        return response

    @app.get('/api/auth/me')
    def current_account(request:Request):
        return {'user':request.state.user}

    @app.post('/api/auth/password')
    def change_password(body:dict, request:Request):
        if request.state.user['id'] in ('service-token','local-development'):
            raise HTTPException(403, 'Sign in with your personal account.')
        return session_response(accounts.change_password(request.state.user['id'], body,
                                request.client.host if request.client else 'unknown'))

    @app.get('/api/auth/users')
    def list_accounts():
        return {'items':accounts.list()}

    @app.post('/api/auth/users')
    def create_account(body:dict, request:Request):
        return accounts.create(body, request.state.user['id'])

    @app.patch('/api/auth/users/{id}')
    def update_account(id:str, body:dict, request:Request):
        return accounts.update(id, body, request.state.user['id'])

    @app.get('/api/auth/audit')
    def account_audit():
        with store.connect() as db:
            return {'items':[dict(zip(('at','actor','action','target'), row)) for row in
                db.execute('SELECT at,actor,action,target FROM auth_audit ORDER BY id DESC LIMIT 100')]}

    @app.get('/api/clusters')
    def clusters_list():
        from .clusters import public
        with service.store.connect() as db:
            return {'items':[public(c) for c in service.store.all(db,'cluster')]}

    @app.post('/api/clusters')
    def cluster_create(body:dict):
        from .clusters import create
        if mode=='readonly':raise HTTPException(403,'Read-only mode')
        return create(service.store,body)

    @app.post('/api/clusters/{id}/{action}')
    def cluster_action(id:str,action:str,body:dict):
        from .clusters import rotate,control,adopt,authorize
        if mode=='readonly':raise HTTPException(403,'Read-only mode')
        if action=='manifest':
            from .agent import validate_server
            c=service.get('cluster',id)
            credential=body.get('bootstrap_token','')
            authorize(c,credential,True)
            server=validate_server(body.get('server_url',''))
            template=(ROOT/'deploy'/'agent.example.yaml').read_text()
            for key,value in [('https://REPLACE_WITH_CENTRAL_SERVER',server),('REPLACE_WITH_CLUSTER_ID',id),('REPLACE_WITH_ONE_TIME_TOKEN',credential)]:
                template=template.replace(json.dumps(key),json.dumps(value))
            return {'manifest':template}
        if action=='rotate':return rotate(service.store,id)
        if action=='adopt':return adopt(service.store,id,body.get('node_uid'))
        return control(service.store,id,action)

    @app.post('/api/agent/enroll')
    def agent_enroll(request:Request,body:dict):
        from .clusters import enroll
        return enroll(service.store,body,request.headers.get('authorization','').removeprefix('Bearer '))

    @app.post('/api/agent/snapshot')
    def agent_snapshot(request:Request,body:dict):
        from .clusters import snapshot
        return snapshot(service.store,body,request.headers.get('authorization','').removeprefix('Bearer '))

    @app.get('/api/workshop')
    def workshop():
        from .workshop import topology
        return topology(service)

    @app.post('/api/whales')
    def add_whale(body:dict):
        from .workshop import create_whale
        return create_whale(service,body)

    @app.post('/api/whales/{id}/pinocchios')
    def add_pinocchio(id:str,body:dict):
        from .workshop import create_pinocchio
        return create_pinocchio(service,id,body)

    @app.patch('/api/whales/{id}')
    def rename_whale(id:str,body:dict):
        from .workshop import rename
        return rename(service,'whale',id,body)

    @app.patch('/api/pinocchios/{id}')
    def rename_pinocchio(id:str,body:dict):
        from .workshop import rename
        return rename(service,'pinocchio',id,body)

    @app.post('/api/projects')
    def add_project(body:dict):
        from .workshop import create_project
        return create_project(service,body)

    @app.patch('/api/projects/{id}')
    def rename_project(id:str,body:dict):
        from .workshop import rename
        return rename(service,'project',id,body)

    @app.post('/api/pinocchios/{id}/project')
    def change_project(id:str,body:dict):
        from .workshop import move_project
        return move_project(service,id,body)

    @app.get('/api/sdk/package')
    def sdk_package():
        wheels=sorted((ROOT/'dist').glob('geppetto_sdk-*.whl'))
        if not wheels:raise HTTPException(404,'SDK package is not built')
        path=wheels[-1]
        return FileResponse(path,filename=path.name,media_type='application/octet-stream')

    @app.get('/api/sdk/guide')
    def sdk_guide():
        return FileResponse(ROOT/'docs/PYTHON_SDK.md',filename='GEPPETTO_PYTHON_SDK.md',media_type='text/markdown')

    @app.get('/api/sdk/tokens')
    def sdk_tokens():
        with store.connect() as db:
            return {'items':[{k:v for k,v in item.items() if k!='token_hash'} for item in store.all(db,'sdk_token')]}

    @app.post('/api/sdk/tokens')
    def sdk_issue(body:dict):
        from .tracking import issue
        return issue(service,body)

    @app.post('/api/sdk/tokens/{id}/revoke')
    def sdk_revoke(id:str,body:dict):
        if mode=='readonly':raise HTTPException(403,'Read-only mode')
        with store.connect() as db:
            item=store.get(db,'sdk_token',id);item['revoked']=True;store.put(db,'sdk_token',item)
        return {'revoked':True}

    def sdk_credential(request):
        return request.headers.get('authorization','').removeprefix('Bearer ')

    @app.post('/api/tracking/runs')
    def tracking_init(request:Request,body:dict):
        from .tracking import init
        return init(service,body,sdk_credential(request))

    @app.post('/api/tracking/runs/{id}/inspect')
    def tracking_inspect(id:str,request:Request,body:dict):
        from .tracking import owned,response
        with store.connect() as db:return response(owned(store,db,id,sdk_credential(request)))

    @app.post('/api/tracking/runs/{id}/events')
    def tracking_event(id:str,request:Request,body:dict):
        from .tracking import event
        return event(service,id,body,sdk_credential(request))

    @app.post('/api/tracking/runs/{id}/artifacts')
    def tracking_artifact(id:str,request:Request,body:dict):
        from .tracking import artifact
        return artifact(service,home,id,body,sdk_credential(request))

    @app.post('/api/tracking/runs/{id}/evaluation')
    def sdk_evaluation(id:str,request:Request,body:dict):
        from .tracking import owned
        from .analytics import evaluation
        with service.store.connect() as db:
            owned(service.store,db,id,sdk_credential(request))
        return evaluation(service,{**body,'run_id':id})

    @app.post('/api/tracking/runs/{id}/visualizations')
    def sdk_visualization(id:str,request:Request,body:dict):
        from .tracking import owned
        from .visualizations import publish
        with service.store.connect() as db:
            owned(service.store,db,id,sdk_credential(request))
        return publish(service,id,body)

    @app.post('/api/runs/{id}/visualizations')
    def publish_visualization(id:str,body:dict):
        from .visualizations import publish
        return publish(service,id,body)

    @app.get('/api/runs/{id}/visualizations')
    def run_visualizations(id:str):
        service.get('run',id)
        return {'items':[v for v in service.list('visualization',limit=100000)['items'] if v['run_id']==id]}

    @app.get('/api/registry')
    def registry_versions():
        from .registry import versions
        return {'versions':versions(service),'audit':service.list('registry_audit',limit=1000)['items']}

    @app.post('/api/registry/preview')
    def registry_preview(body:dict):
        from .registry import preview
        return preview(service,body)

    @app.post('/api/registry/aliases')
    def registry_promote(body:dict):
        from .registry import promote
        return promote(service,body)

    @app.post('/api/runs/{id}/retry')
    def retry_run(id:str, body:dict):
        from .recovery import retry
        return retry(service,id,body)

    @app.post('/api/schedules/{id}/backfill/preview')
    def backfill_preview(id:str, body:dict):
        from .backfill import preview
        return preview(service,id,body)

    @app.post('/api/schedules/{id}/backfill')
    def backfill_create(id:str, body:dict):
        from .backfill import create
        return create(service,id,body)

    @app.get('/api/resources/policies')
    def resource_policies():
        return service.list('resource_policy',limit=10000)

    @app.post('/api/projects/{id}/resources')
    def resource_policy(id:str, body:dict):
        from .resource_policy import configure
        return configure(service,id,body)

    @app.get('/api/whales/{id}/resources')
    def node_resources(id:str):
        from .node_metrics import view
        return view(service,id)

    @app.get('/api/pipelines')
    def pipelines():
        return service.list('pipeline',limit=1000)

    @app.post('/api/pipelines/preview')
    def pipeline_preview(body:dict):
        from .pipelines import preview
        return preview(service,body)

    @app.post('/api/pipelines')
    def pipeline_create(body:dict):
        from .pipelines import create
        return create(service,body)

    @app.post('/api/pipelines/{id}/cancel')
    def pipeline_cancel(id:str):
        from .pipelines import cancel
        return cancel(service,id)

    @app.get('/api/training/options')
    def training_options():
        from .training_drafts import options
        return options(service)

    @app.get('/api/execution/profiles')
    def execution_profiles():
        return service.list('execution_profile',limit=100000)

    @app.post('/api/whales/{id}/execution')
    def execution_profile(id:str,body:dict):
        from .execution import configure
        return configure(service,id,body)

    @app.post('/api/datasets/{id}/locations')
    def dataset_location(id:str,body:dict):
        from .execution import bind_dataset
        return bind_dataset(service,id,body)

    @app.post('/api/agent/jobs/poll')
    def agent_jobs(request:Request,body:dict):
        from .execution import poll
        return poll(service,body,request.headers.get('authorization','').removeprefix('Bearer '))

    @app.post('/api/agent/jobs/observe')
    def agent_observation(request:Request,body:dict):
        from .execution import observe
        return observe(service,body,request.headers.get('authorization','').removeprefix('Bearer '))

    @app.get('/api/platform')
    def platform():
        result={kind:service.list(kind,limit=1000)['items'] for kind in
                ('execution_profile','dataset_verification','metric_definition','analysis_report','sweep')}
        result['analysis_report']=[{k:v for k,v in r.items() if k!='samples'} for r in result['analysis_report']]
        result['sweep']=[{**s,'trials':[{'run_id':t['run_id'],'parameters':t['parameters']} for t in s['trials']]} for s in result['sweep']]
        return result

    @app.post('/api/datasets/{id}/verify')
    def verify_dataset(id:str,body:dict):
        from .dataset_versions import request as verify
        return verify(service,id)

    @app.get('/api/datasets/{id}/lineage')
    def dataset_lineage(id:str):
        from .analytics import lineage
        return lineage(service,id)

    @app.post('/api/metrics/definitions')
    def metric_definition(body:dict):
        from .analytics import define_metric
        return define_metric(service,body)

    @app.patch('/api/runs/{id}/annotations')
    def run_annotations(id:str,body:dict):
        from .analytics import annotate
        return annotate(service,id,body)

    @app.post('/api/analysis/reports')
    def analysis_report(body:dict):
        from .analytics import evaluation
        return evaluation(service,body)

    @app.get('/api/analysis/reports/{id}')
    def analysis_details(id:str,slice:str='',errors_only:bool=False,offset:int=0,limit:int=50):
        from .analytics import analyze
        return analyze(service.get('analysis_report',id),slice,errors_only,offset,limit)

    @app.post('/api/sweeps/preview')
    def sweep_preview(body:dict):
        from .sweeps import preview
        return preview(service,body)

    @app.post('/api/sweeps')
    def sweep_create(body:dict):
        from .sweeps import create
        return create(service,body)

    @app.get('/api/sweeps/{id}')
    def sweep_status(id:str):
        from .sweeps import status
        return status(service,id)

    @app.post('/api/sweeps/{id}/cancel')
    def sweep_cancel(id:str,body:dict):
        from .sweeps import cancel
        return cancel(service,id)

    @app.post('/api/training/preview')
    def training_preview(body:dict):
        from .training_drafts import validate
        return validate(service,body)

    @app.post('/api/training/drafts')
    def training_save(body:dict):
        from .training_drafts import save
        return save(service,body)

    @app.get('/api/status')
    def status():
        return {'mode':mode,'categories':CATEGORIES,'worker_error':app.state.worker_error,
                'gpu':{'uuid_verified':False,'devices':[{'uuid':'GPU-DEMO-0','name':'Simulated GPU · Not a physical device'}] if mode=='demo' else []},
                'capabilities':{'submit':mode in ('demo','live') or mode=='catalog' and any(p['enabled'] for p in service.list('execution_profile',limit=100000)['items']),'resume':False,'upload':False,'onnx':False},
                'integrations':{'airflow':bool(os.environ.get('GEPPETTO_AIRFLOW_URL')),
                                'mlflow':bool(os.environ.get('GEPPETTO_MLFLOW_URL'))},'updated_at':time.time()}

    @app.post('/api/pinocchios/{id}/delete')
    def delete_model(id:str,body:dict,request:Request):
        from .model_history import change
        return change(service,id,body,request.state.user,'delete')

    @app.post('/api/run-history/delete')
    def delete_history(body:dict, request:Request):
        from .run_history import change
        return change(service, body, request.state.user, 'delete')

    @app.post('/api/run-history/restore')
    def restore_history(body:dict, request:Request):
        from .run_history import change
        return change(service, body, request.state.user, 'restore')

    @app.get('/api/catalog/{kind}')
    def listing(kind:str,category:str|None=None,offset:int=0,limit:int=50,visibility:str="active"):
        if kind not in ('template','dataset','run','schedule','whale','pinocchio'):
            raise HTTPException(404)
        if offset < 0 or not 1 <= limit <= 100:
            raise HTTPException(422,'Invalid pagination')
        if kind == "pinocchio":
            from .workshop import topology
            items=[p for p in topology(service)["pinocchio"] if not category or p["category"]==category]
            return {"items":items[offset:offset+limit],"total":len(items),"offset":offset,"limit":limit}
        if kind == "run":
            from .run_history import page
            return page(service,category,offset,limit,visibility)
        return service.list(kind,category,offset,limit)

    @app.get('/api/catalog/{kind}/{id}')
    def detail(kind:str,id:str):
        if kind not in ('template','dataset','run','schedule','whale','pinocchio'):
            raise HTTPException(404)
        item=service.get(kind,id)
        if kind=='run':
            from .run_history import detail as history_detail
            item=history_detail(service,item)
            item['links']=[]
            ml_ui=os.environ.get('GEPPETTO_MLFLOW_UI_URL')
            af_ui=os.environ.get('GEPPETTO_AIRFLOW_UI_URL')
            if ml_ui and urlparse(ml_ui).scheme in ('http','https'):
                for ref in item['mlflow']:
                    if ref.get('experiment_id'):
                        item['links'].append({'name':'MLflow details','url':ml_ui.rstrip('/')+'/#/experiments/'+quote(ref['experiment_id'],safe='')+'/runs/'+quote(ref['run_id'],safe='')})
            if af_ui and urlparse(af_ui).scheme in ('http','https'):
                for ref in item['airflow']:
                    item['links'].append({'name':'Airflow details','url':af_ui.rstrip('/')+'/dags/'+quote(ref['dag_id'],safe='')+'/runs/'+quote(ref['run_id'],safe='')})
        return item

    @app.post('/api/datasets')
    def register(body:dict):
        if mode=='readonly':
            raise HTTPException(409,'Read-only mode')
        return service.register_dataset(body)

    @app.post('/api/preview')
    def preview(body:dict):
        return service.preview(body)

    @app.post('/api/submissions')
    def submit(body:dict,request:Request):
        return service.submit(body,request.headers.get('idempotency-key',''))

    @app.post('/api/runs/{id}/stop')
    def stop(id:str,body:dict):
        return service.stop(id,body['name'])

    @app.patch('/api/schedules/{id}')
    def schedule(id:str,body:dict):
        return service.edit_schedule(id,body)

    @app.post('/api/compare')
    def compare(body:dict):
        return service.compare(body['ids'])

    @app.post('/api/integrations/sync')
    def sync(body:dict):
        from .adapters import MLflow,sync_mlflow
        url=os.environ.get('GEPPETTO_MLFLOW_URL')
        if not url:
            raise HTTPException(409,'MLflow connection is not configured')
        mapping=json.loads(os.environ.get('GEPPETTO_EXPERIMENT_MAP','{}'))
        if not mapping:
            raise HTTPException(409,'Explicit experiment/category mapping is required')
        client=MLflow(url,os.environ.get('GEPPETTO_MLFLOW_TOKEN'))
        try:
            return {'imported':sync_mlflow(service,client,mapping, json.loads(os.environ['GEPPETTO_MLFLOW_RUN_IDS']) if os.environ.get('GEPPETTO_MLFLOW_RUN_IDS') else None)}
        except Exception as exc:
            # Keep existing observations and states, record retrieval failure separately.
            with service.store.connect() as db:
                for run in service.store.all(db,'run'):
                    if run.get('imported'):
                        run['fetch_error']=type(exc).__name__
                        run['observed_at']=time.time()
                        service.store.put(db,'run',run)
            raise HTTPException(502,'MLflow query failed. The last successful record is preserved.') from exc
        finally:
            client.client.close()

    def safe_response(root,path,expected=None,download=False,download_name=None):
        spool=tempfile.SpooledTemporaryFile(max_size=8*1024*1024)
        sha=hashlib.sha256()
        try:
            with open_beneath(root,path) as source:
                while chunk:=source.read(1024*1024):
                    sha.update(chunk)
                    spool.write(chunk)
            if expected and (sha.hexdigest()!=expected['sha256'] or spool.tell()!=expected['size']):
                raise HTTPException(409,'File contents changed after completion.')
            spool.seek(0)
        except BaseException:
            spool.close()
            raise
        def stream():
            try:
                while data:=spool.read(1024*1024):
                    yield data
            finally:
                spool.close()
        media=mimetypes.guess_type(path)[0] or 'application/octet-stream'
        if not download and media not in ('image/png','image/jpeg','image/webp','application/json','text/plain','text/csv','text/tab-separated-values'):
            spool.close()
            raise HTTPException(415,'Preview format is not supported')
        return StreamingResponse(stream(),media_type=media,
              headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(Path(download_name or path).name,safe='')} if download else {})

    @app.get('/api/runs/{id}/gradcam')
    def gradcam_options(id:str):
        from .gradcam import options
        return options(service,id)

    @app.post('/api/runs/{id}/gradcam/preview')
    def gradcam_preview(id:str,body:dict):
        from .gradcam import preview
        return preview(service,home,id,body)

    @app.post('/api/runs/{id}/gradcam')
    def gradcam_submit(id:str,body:dict,request:Request):
        from .gradcam import submit
        return submit(service,home,id,body,request.headers.get('idempotency-key',''))

    @app.get('/api/runs/{id}/error-analysis')
    def error_analysis(id:str,report_id:str='',confidence:float|None=None,iou:float=.5,camera:str='',kind:str='fn',cell:int|None=None,offset:int=0,sort:str='errors'):
        from .error_analysis import analyze
        service.get('run',id)
        with service.store.connect() as db:
            reports=[r for r in service.store.all(db,'evaluation') if r['run_id']==id]
        if not reports:
            return {'available':False,'reports':[]}
        report=next((r for r in reports if r['id']==report_id),None) if report_id else reports[0]
        if not report:
            raise HTTPException(404,'Evaluation report not found')
        confidence=max(.5,report['prediction_floor']) if confidence is None else confidence
        return {'available':True,'applied_confidence':confidence,'reports':[{'id':r['id'],'checkpoint':r['checkpoint'],'split':r['split'],'prediction_floor':r['prediction_floor']} for r in reports],
                **analyze(report,confidence,iou,camera,kind,cell,offset,sort)}

    @app.post('/api/runs/{id}/error-analysis')
    def import_error_analysis(id:str,body:dict):
        from .error_analysis import import_evaluation
        if mode=='readonly':
            raise HTTPException(403,'Read-only mode')
        return import_evaluation(service,id,body)

    @app.post('/api/runs/{id}/error-analysis/demo')
    def demo_error_analysis(id:str,body:dict):
        from .error_analysis import import_evaluation,source_sample
        run=service.get('run',id)
        if mode!='demo' or not run.get('synthetic') or not any(d['id']=='demo-ball-detection' for d in run.get('frozen',{}).get('datasets',[])):
            raise HTTPException(409,'Synthetic ball fixture runs only')
        dataset=service.get('dataset','demo-ball-detection')
        rows=[]
        for index in (4,5):
            g=source_sample(dataset,index)['truth'][0]
            rows.append({'index':index,'predictions':([{'box':g,'score':.85}] if index==4 else [])+
                         [{'box':[.15,.2,.05,.05],'score':.65}]})
        return import_evaluation(service,id,{'dataset_id':dataset['id'],'dataset_version':dataset['version'],
            'split':'validation','checkpoint':'SYNTHETIC UI EXAMPLE — not model inference','prediction_floor':0,'images':rows})

    @app.get('/api/runs/{id}/error-analysis/{report_id}/images/{index}')
    def evaluation_image(id:str,report_id:str,index:int):
        report=service.get('evaluation',report_id)
        if report['run_id']!=id:
            raise HTTPException(404)
        row=next((r for r in report['records'] if r['index']==index),None)
        if not row:
            raise HTTPException(404)
        dataset=service.get('dataset',report['dataset_id'])
        return safe_response(dataset['root'],row['image'],row['image_file'])

    @app.get('/api/datasets/{id}/samples')
    def samples(id:str,split:str|None=None,offset:int=0,limit:int=12):
        if split not in (None,'train','validation','test') or offset<0 or not 1<=limit<=48:
            raise HTTPException(422,'Invalid sample pagination')
        return sample_page(service.get('dataset',id),split,offset,limit)

    @app.get('/api/datasets/{id}/samples/{index}/{role}')
    def sample(id:str,index:int,role:str):
        dataset=service.get('dataset',id)
        if role not in ('image','label'):
            raise HTTPException(404)
        sample=sample_at(dataset,index)
        if role=='label' and dataset.get('policy',{}).get('format')=='jersey-number' and 'number' in sample:
            return JSONResponse({'number':sample['number'],'source':sample.get('source'),'split':sample['split']})
        expected=sample.get(role+'_file')
        if not sample.get(role):
            raise HTTPException(404)
        return safe_response(dataset['root'],sample[role],expected)

    @app.get('/api/runs/{id}/artifacts/{artifact_id}')
    def artifact(id:str,artifact_id:str,download:bool=True):
        run=service.get('run',id)
        found=next((a for a in run['artifacts'] if a['id']==artifact_id),None)
        if not found or not found['complete'] or run['state'] not in ('succeeded','failed','stopped'):
            raise HTTPException(409,'Only completed files can be downloaded.')
        if found.get('remote'):
            from .remote_files import fetch_completed
            root,path,expected=fetch_completed(found,home/'artifact-cache',os.environ.get('GEPPETTO_MLFLOW_URL'),os.environ.get('GEPPETTO_MLFLOW_TOKEN'))
            return safe_response(root,path,expected,download,found['path'])
        return safe_response(found['root'],found['path'],found,download)

    app.mount('/static',StaticFiles(directory=ROOT/'web'),name='static')

    @app.get('/')
    def index():
        return FileResponse(ROOT/'web/index.html')

    return app


def main():
    import argparse
    import uvicorn
    parser=argparse.ArgumentParser()
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8091)
    args=parser.parse_args()
    if args.host not in ('127.0.0.1','localhost','::1') and not os.environ.get('GEPPETTO_TOKEN'):
        parser.error('Non-loopback binding requires GEPPETTO_TOKEN and GEPPETTO_ALLOWED_HOSTS')
    uvicorn.run(create_app(),host=args.host,port=args.port)


if __name__=='__main__':
    main()
