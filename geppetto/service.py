"""Catalog, durable submissions and scheduler. All external effects live in adapters."""
import copy
from datetime import datetime, timezone
import json
import re
import time
import uuid
from zoneinfo import ZoneInfo

from croniter import croniter
from jsonschema import Draft202012Validator

from .catalog import CATEGORIES, templates
from .configuration import digest, merge, compile_preview
from .files import allowed_root, manifest, verify_manifest, fingerprint
from .storage import Store

TERMINAL = {'succeeded', 'failed', 'stopped', 'skipped'}


def identifier():
    return uuid.uuid4().hex


def next_time(expression, zone, after):
    if len(expression.split()) != 5 or not croniter.is_valid(expression):
        raise ValueError('Use a five-field cron expression')
    return croniter(expression, datetime.fromtimestamp(after, ZoneInfo(zone))).get_next(datetime).timestamp()


class Service:
    def __init__(self, store, mode='demo', roots=(), concurrency=1):
        if mode not in ('demo', 'readonly', 'live', 'catalog'):
            raise ValueError('Mode must be demo, readonly, live or catalog')
        self.store, self.mode, self.roots = store, mode, roots
        self.concurrency = concurrency
        with store.connect() as db:
            for template in templates():
                store.put(db, 'template', template)

    def get(self, kind, id):
        with self.store.connect() as db:
            return self.store.get(db, kind, id)

    def list(self, kind, category=None, offset=0, limit=50):
        with self.store.connect() as db:
            items = self.store.all(db, kind)
        if category:
            items = [i for i in items if i.get('category') == category]
        return {'items': items[offset:offset+limit], 'total': len(items), 'offset': offset, 'limit': limit}

    def register_dataset(self, body):
        if body['category'] not in {c['id'] for c in CATEGORIES}:
            raise ValueError('Unknown category')
        from .workshop import LOCAL_WHALE,reconcile
        reconcile(self.store)
        node=self.get('whale',body.get('node_id',LOCAL_WHALE))
        if node['id']!=LOCAL_WHALE:
            raise ValueError('Remote node dataset access is not configured')
        root = allowed_root(body['root'], self.roots)
        samples = manifest(root, body['samples'])
        sources=[fingerprint(root,path) for path in sorted(set(body.get('source_files',[])))]
        version = digest({'samples': samples, 'source_files':sources,'policy': body.get('policy', {})})
        item = {'id': identifier(), 'name': body['name'], 'category': body['category'], 'root': root,
                'node_id':node['id'], 'version': version, 'samples': samples, 'source_files':sources,'policy': body.get('policy', {}),
                'created_at': time.time(), 'synthetic': False, 'status': 'registered',
                'version_basis':'content-inline','content_verified':True,
                'effective_samples': None, 'splits': {s: sum(x['split'] == s for x in samples) for s in ('train','validation','test')}}
        with self.store.connect() as db:
            self.store.put(db, 'dataset', item)
        return item

    def freeze(self, body):
        from .workshop import LOCAL_WHALE,reconcile
        reconcile(self.store)
        node=self.get('whale',body.get('node_id',LOCAL_WHALE))
        agent = node['connection']=='kubernetes-agent'
        if agent and self.mode=='demo':
            raise ValueError('Demo mode cannot dispatch real Agent jobs')
        if self.mode=='catalog' and not agent:
            raise ValueError('Verify the execution connection before starting training. Simulated jobs are disabled.')
        if node['connection'] not in ('local-workspace','kubernetes-agent'):
            raise ValueError('Whale is not connected for execution')
        pinocchio=self.get('pinocchio',body['pinocchio_id']) if body.get('pinocchio_id') else None
        if pinocchio and (pinocchio['node_id']!=node['id'] or pinocchio['template_id']!=body['template_id']):
            raise ValueError('Pinocchio, node and model template must match')
        template = self.get('template', body['template_id'])
        datasets = [self.get('dataset', id) for id in body['dataset_ids']]
        from .execution import available
        if template['id'] not in node['template_ids'] or any(not available(d,node['id']) for d in datasets):
            raise ValueError('Select resources inside the chosen whale')
        if not datasets or any(d['category'] != template['category'] for d in datasets):
            raise ValueError('Select compatible dataset versions')
        if len({d['id'] for d in datasets}) != len(datasets):
            raise ValueError('Duplicate dataset version')
        overrides = body.get('overrides', {})
        if template.get('bundle'):
            from .catalog import ROOT
            bundle = json.loads((ROOT / 'contracts' / template['bundle']).read_text())
            managed = {'data': {'root_dir': '/datasets/input', 'cache_dir': '/cache/run'},
                       'logging': {'base_dir': '/outputs', 'experiment_name': 'geppetto'},
                       'wandb': {'enabled': False}}
            config = compile_preview(bundle, overrides, managed)['config']
        else:
            config = merge(template['defaults'], overrides)
            Draft202012Validator(template['schema']).validate(config)
        if template['adapter']=='jersey':
            if config['training']['max_epochs']<=0 or config['data']['batch_size']<=0 or config['training']['learning_rate']<=0:
                raise ValueError('Epochs, batch size and learning rate must be positive')
        gpu = body.get('gpu', {'mode': 'auto', 'count': 1})
        if gpu.get('mode') not in ('auto', 'uuid') or type(gpu.get('count')) is not int or not 1 <= gpu['count'] <= 64:
            raise ValueError('GPU count must be 1–64')
        if gpu['count'] > 1 and (not agent or template['adapter'] != 'ball' or gpu['mode'] != 'auto'):
            raise ValueError('Multi-GPU execution requires the managed ball adapter and automatic allocation')
        if gpu['mode'] == 'uuid' and not re.fullmatch(r'GPU-[A-Za-z0-9-]+', gpu.get('uuid', '')):
            raise ValueError('GPU UUID is required')
        if self.mode != 'demo' and gpu['mode'] == 'uuid':
            raise ValueError('Physical UUID selection awaits isolated GPU proof')
        parent = body.get('parent_id')
        relation = body.get('relation', 'train')
        if relation not in ('train', 'resume', 'evaluate', 'initialize'):
            raise ValueError('Unknown run relation')
        if relation != 'train' and not parent:
            raise ValueError('Parent execution is required')
        if agent and relation not in ('train','resume','evaluate'):
            raise ValueError('This model image supports training and ball evaluation; complete resume awaits validation')
        evaluation=None
        resume=None
        if agent and relation=='evaluate':
            if template['adapter']!='ball':raise ValueError('Managed evaluation is currently available for the ball adapter')
            source=self.get('run',parent)
            if source['state'] not in TERMINAL:raise ValueError('Evaluate a finalized checkpoint')
            checkpoint=next((a for a in source['artifacts'] if a['id']==body.get('checkpoint_id')),None)
            if not checkpoint or not checkpoint.get('complete') or checkpoint.get('role')!='inference_weights' or not checkpoint.get('sha256'):
                raise ValueError('Select a finalized inference checkpoint with a content hash')
            if not checkpoint.get('artifact_uri','').startswith('mlflow-artifacts:/'):
                raise ValueError('Evaluation requires a checkpoint served by MLflow')
            from .execution import relative
            from .analytics import finite
            path=relative(checkpoint['path'])
            policy=body.get('evaluation',{})
            split=policy.get('split','test')
            if split not in ('validation','test') or any(not d.get('splits',{}).get(split) for d in datasets):
                raise ValueError('Select a nonempty validation or test split')
            conf=finite(policy.get('confidence',.001));iou=finite(policy.get('iou',.7))
            if not 0<=conf<=1 or not 0<iou<=1:raise ValueError('Invalid evaluation confidence/IoU')
            evaluation={'checkpoint_uri':checkpoint['artifact_uri'].rstrip('/')+'/'+path,'checkpoint_sha256':checkpoint['sha256'],
                        'split':split,'confidence':conf,'iou':iou,'max_det':300,'precision':'fp32','evaluator_version':template['code_version']}
        if parent:
            original = self.get('run', parent)
            if relation in ('resume','evaluate'):
                if original.get('node_id')!=node['id'] or pinocchio and pinocchio['id']!=original.get('pinocchio_id'):
                    raise ValueError('Resume and evaluation belong to the original Pinocchio and whale')
                pinocchio=self.get('pinocchio',original['pinocchio_id'])
            if original['template_id'] != template['id']:
                raise ValueError('Parent model differs')
            if relation == 'resume':
                if not template['capabilities']['resume'] and not (agent and template['adapter']=='ball'):
                    raise ValueError('Full resume has not been validated for this template')
                if original['state'] not in ('failed','stopped'):
                    raise ValueError('Only interrupted runs can resume')
                if not any(a.get('resume') and a.get('complete') for a in original.get('artifacts', [])):
                    raise ValueError('No completed resume checkpoint')
                config = copy.deepcopy(original['frozen']['config'])
                if agent:
                    if gpu != original['frozen']['gpu']:
                        raise ValueError('Resume requires the original GPU topology')
                    original_datasets=[{'id':d['id'],'version':d['version']} for d in original['frozen']['datasets']]
                    if original['frozen']['template']['code_version']!=template['code_version'] or original_datasets!=[{'id':d['id'],'version':d['version']} for d in datasets]:
                        raise ValueError('Resume requires unchanged code and dataset versions')
                    checkpoint=next((a for a in original['artifacts'] if a.get('resume') and a.get('complete') and a.get('role')=='resume_checkpoint'),None)
                    if not checkpoint or not checkpoint.get('sha256') or not checkpoint.get('artifact_uri','').startswith('mlflow-artifacts:/'):
                        raise ValueError('Managed resume checkpoint required')
                    from .execution import relative
                    resume={'parent_id':parent,'checkpoint_uri':checkpoint['artifact_uri'].rstrip('/')+'/'+relative(checkpoint['path']),
                            'checkpoint_sha256':checkpoint['sha256']}
                    best=next((a for a in original['artifacts'] if a.get('complete') and a.get('role')=='inference_weights' and a['path'].endswith('/best.pt')),None)
                    if best:
                        resume.update(best_uri=best['artifact_uri'].rstrip('/')+'/'+relative(best['path']),best_sha256=best['sha256'])
                    if any(r.get('pinocchio_id')==original.get('pinocchio_id') and r['state'] not in TERMINAL and r.get('frozen',{}).get('relation')!='evaluate' for r in self.list('run',limit=100000)['items']):
                        raise ValueError('A child execution is already active')
        if self.mode == 'readonly':
            raise ValueError('Read-only mode does not accept new executions')
        if self.mode == 'live' and not agent and (not template['live_verified'] or not template['image_digest']):
            raise ValueError('Template image and execution adapter must be validated before live submission')
        if self.mode != 'demo' and any(d.get('synthetic') for d in datasets):
            raise ValueError('Synthetic datasets cannot be used for live execution')
        if self.mode=='live' and any(d.get('status')=='indexed' for d in datasets):
            raise ValueError('Indexed datasets require full content verification before actual training')
        if pinocchio:
            from .model_history import ensure_visible
            with self.store.connect() as db:ensure_visible(self.store,db,pinocchio['id'])
        project_id=pinocchio.get('project_id') if pinocchio else body.get('project_id','project-'+template['category'])
        if self.get('project',project_id)['category']!=template['category']:
            raise ValueError('Project category differs from the model template')
        model_name=pinocchio['name'] if pinocchio else template['name']
        if pinocchio and relation in ('train','initialize') and any(r.get('pinocchio_id')==pinocchio['id'] for r in self.list('run',limit=100000)['items']):
            pinocchio=None
        frozen = {'node_id':node['id'],'pinocchio_id':pinocchio['id'] if pinocchio else None,'project_id':project_id,'model_name':model_name,'template': template, 'config': config, 'config_sha256': digest(config),
                  'schema_sha256':digest(template['schema']),
                  'datasets': [{'id': d['id'], 'name': d['name'], 'version': d['version']} for d in datasets],
                  'gpu': gpu, 'relation': relation, 'parent_id': parent,
                  'evaluation_policy': [d['policy'] for d in datasets]}
        priority=body.get('priority',0)
        if type(priority) is not int or not -100<=priority<=100:raise ValueError('Priority must be -100–100')
        frozen['priority']=priority
        from .recovery import policy
        frozen['retry'] = policy(body.get('retry'))
        frozen['preemptible'] = body.get('preemptible') is True
        if agent:
            from .execution import freeze
            frozen['execution']=freeze(self,node,template,datasets)
            profile = frozen['execution']['profile']
            if gpu['count'] > int(node['node'].get('allocatable', {}).get('nvidia.com/gpu', 0)):
                raise ValueError('Requested GPU count exceeds the selected node capacity')
            if gpu['count'] > profile.get('max_gpus', 1):
                raise ValueError('Enable a distributed-capable image in the node execution profile first')
            if gpu['count'] > 1 and relation == 'evaluate':
                raise ValueError('Evaluation uses one GPU')
            if gpu['count'] > 1 and config.get('batch', 1) % gpu['count']:
                raise ValueError('Global batch size must be divisible by the GPU count')
        if agent:
            from .distributed import freeze as freeze_distributed
            freeze_distributed(self, frozen)
        if resume:
            if frozen['execution']['profile']['claims']!=original['frozen']['execution']['profile']['claims']:
                raise ValueError('Resume requires the original storage claims')
            frozen['execution']=copy.deepcopy(original['frozen']['execution'])
            frozen['resume']=resume
        if evaluation:
            frozen['evaluation']=evaluation
            frozen['evaluation_policy']=[{k:v for k,v in evaluation.items() if not k.startswith('checkpoint_')}]
        return frozen

    def preview(self, body):
        frozen = self.freeze(body)
        return {'frozen': frozen, 'confirmation_hash': self.confirmation(frozen,body), 'mode': self.mode}

    @staticmethod
    def confirmation(frozen,body):
        return digest({'frozen':frozen,'name':body['name'], 'schedule':body.get('schedule',{'kind':'now'})})

    def submit(self, body, key):
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', key):
            raise ValueError('Idempotency-Key must contain 8–100 safe characters')
        request_hash = digest(body)
        with self.store.connect() as db:
            existing = db.execute('SELECT digest,kind,id FROM requests WHERE key=?', (key,)).fetchone()
            if existing:
                if existing[0] != request_hash:
                    raise ValueError('Idempotency key already used with different settings')
                return self.store.get(db, existing[1], existing[2])
        frozen = self.freeze(body)
        if body.get('confirmation_hash') != self.confirmation(frozen,body):
            raise ValueError('Review the current final configuration before submitting')
        schedule = body.get('schedule', {'kind': 'now'})
        now = time.time()
        kind = schedule.get('kind')
        zone = schedule.get('timezone', 'UTC')
        ZoneInfo(zone)
        if kind not in ('now','once','cron','after'):
            raise ValueError('Invalid schedule kind')
        due = now
        if kind == 'once':
            parsed = datetime.fromisoformat(schedule['at'])
            if parsed.tzinfo is None:
                raise ValueError('Scheduled timestamp requires an explicit UTC offset')
            due = parsed.timestamp()
            if due < now:
                raise ValueError('Scheduled date is in the past')
        elif kind == 'cron':
            due = next_time(schedule['cron'], zone, now)
        elif kind == 'after':
            dependency = self.get('run', schedule['run_id'])
            if dependency['category'] != frozen['template']['category']:
                raise ValueError('Dependency must be in the same category')
        item = {'id': identifier(), 'name': body['name'].strip()[:160] or 'New training',
                'category': frozen['template']['category'], 'template_id': body['template_id'],
                'frozen': frozen, 'created_at': now, 'synthetic': self.mode == 'demo'}
        item['priority']=frozen.get('priority',0)
        if kind == 'now':
            item = self.new_run(item)
            document_kind = 'run'
        else:
            item.update({'schedule': schedule, 'next_at': due, 'timezone': zone, 'enabled': True,
                         'overlap_policy': 'skip', 'misfire_policy': 'coalesce', 'priority': 0, 'history': []})
            document_kind = 'schedule'
        with self.store.connect() as db:
            # Recheck after validation for concurrent retries.
            existing = db.execute('SELECT digest,kind,id FROM requests WHERE key=?', (key,)).fetchone()
            if existing:
                if existing[0] != request_hash:
                    raise ValueError('Idempotency conflict')
                return self.store.get(db, existing[1], existing[2])
            from .workshop import new_pinocchio
            pid=frozen.get('pinocchio_id')
            from .model_history import ensure_visible
            ensure_visible(self.store,db,pid)
            if pid and frozen.get('relation')=='resume' and any(r.get('pinocchio_id')==pid and r['state'] not in TERMINAL and r.get('frozen',{}).get('relation')!='evaluate' for r in self.store.all(db,'run')):
                raise ValueError('This model already has an active training execution')
            if pid and frozen.get('relation','train') in ('train','initialize') and any(r.get('pinocchio_id')==pid for r in self.store.all(db,'run')):
                pid=None
            if not pid:
                new=new_pinocchio(self.store,db,frozen['node_id'],frozen['template'],frozen.get('model_name',''))
                new['project_id']=frozen.get('project_id') or new['project_id']
                self.store.put(db,'pinocchio',new);pid=new['id']
            pin=self.store.get(db,'pinocchio',pid)
            item.update(node_id=frozen['node_id'],pinocchio_id=pid,model_id=pid,project_id=pin.get('project_id'))
            self.store.put(db, document_kind, item)
            db.execute('INSERT INTO requests VALUES(?,?,?,?)', (key, request_hash, document_kind, item['id']))
        return item

    @staticmethod
    def new_run(item):
        return {**item, 'state': 'queued', 'phase': 'Waiting for resources', 'metrics': {}, 'artifacts': [],
                'logs': [], 'airflow': [], 'mlflow': [], 'stop_requested_at': None,
                'observed_at': None, 'last_success_at': None, 'fetch_error': None,
                'progress': {'epoch': 0, 'step': 0, 'total': None}, 'attempt': 0}

    def stop(self, id, name):
        with self.store.connect() as db:
            run = self.store.get(db, 'run', id)
            if run['name'] != name:
                raise ValueError('Confirm the exact execution name')
            if run.get('imported') or self.mode == 'readonly':
                raise ValueError('Imported executions are read-only')
            if run.get('retry_at') and not run.get('retry_run_id'):
                run.pop('retry_at', None)
                run['stop_requested_at'] = time.time()
                run['phase'] = 'Automatic retry canceled'
                self.store.put(db, 'run', run)
            if run['state'] not in TERMINAL:
                run['stop_requested_at'] = time.time()
                # Outbox intent persists; only worker acknowledges resource release.
                self.store.put(db, 'run', run)
            return run

    def edit_schedule(self, id, body):
        with self.store.connect() as db:
            item = self.store.get(db, 'schedule', id)
            if self.mode == 'readonly':
                raise ValueError('Read-only mode')
            if 'enabled' in body:
                item['enabled'] = bool(body['enabled'])
            if body.get('cancel'):
                item['enabled'] = False
                item['cancelled_at'] = time.time()
            if item['enabled']:
                from .model_history import ensure_visible
                ensure_visible(self.store,db,item.get('pinocchio_id'))
            if item.get('cancelled_at') and item['enabled']:
                raise ValueError('Cancelled schedules cannot be reactivated')
            if 'cron' in body:
                if item['schedule']['kind'] != 'cron':
                    raise ValueError('Only recurring schedules accept cron edits')
                item['schedule']['cron'] = body['cron']
                item['timezone'] = body.get('timezone', item['timezone'])
                item['schedule']['timezone'] = item['timezone']
                item['next_at'] = next_time(body['cron'], item['timezone'], time.time())
            if 'at' in body:
                if item['schedule']['kind']!='once':
                    raise ValueError('Only one-time schedules accept a date')
                parsed=datetime.fromisoformat(body['at'])
                if parsed.tzinfo is None or parsed.timestamp()<=time.time():
                    raise ValueError('Use a future timestamp with explicit UTC offset')
                item['schedule']['at']=body['at']
                item['next_at']=parsed.timestamp()
                item['timezone']=body.get('timezone',item['timezone'])
                ZoneInfo(item['timezone'])
                item['schedule']['timezone']=item['timezone']
            self.store.put(db, 'schedule', item)
            return item

    def tick_schedules(self, db, now):
        for schedule in self.store.all(db, 'schedule'):
            if not schedule['enabled'] or schedule['next_at'] > now:
                continue
            spec = schedule['schedule']
            if spec['kind'] == 'after':
                dependency = self.store.get(db, 'run', spec['run_id'])
                if dependency['state'] not in TERMINAL:
                    continue
                if dependency['state'] != 'succeeded':
                    schedule['enabled'] = False
                    schedule['history'].append({'at': now, 'result': 'dependency_failed'})
                    self.store.put(db, 'schedule', schedule)
                    continue
            active = any(r.get('schedule_id') == schedule['id'] and r['state'] not in TERMINAL for r in self.store.all(db, 'run'))
            occurrence = digest({'schedule': schedule['id'], 'at': schedule['next_at']})[:32]
            if any(r['id'] == occurrence for r in self.store.all(db, 'run')):
                schedule['history'].append({'at':now, 'result':'already_exists', 'run_id':occurrence})
            elif active:
                schedule['history'].append({'at': now, 'result': 'skipped_overlap'})
            else:
                # Stable occurrence identity + same transaction prevents restart duplication.
                run_id = digest({'schedule': schedule['id'], 'at': schedule['next_at']})[:32]
                run = self.new_run({k: copy.deepcopy(schedule[k]) for k in ('name','category','template_id','frozen','synthetic')})
                run.update({'id': run_id, 'created_at': now, 'schedule_id': schedule['id'],
                            'node_id':schedule.get('node_id','whale-local'),'pinocchio_id':schedule.get('pinocchio_id'),
                            'project_id':schedule['frozen'].get('project_id'), 'priority':schedule['frozen'].get('priority',0),
                            'logical_date':schedule['next_at']})
                from .workshop import new_pinocchio
                if any(r.get('pinocchio_id')==run.get('pinocchio_id') for r in self.store.all(db,'run')):
                    pin=new_pinocchio(self.store,db,run['node_id'],run['frozen']['template'])
                    pin['project_id']=run['frozen'].get('project_id') or pin['project_id']
                    self.store.put(db,'pinocchio',pin)
                    run['pinocchio_id']=pin['id']
                run['model_id']=run['pinocchio_id']
                self.store.put(db, 'run', run)
                schedule['history'].append({'at': now, 'result': 'created', 'run_id': run_id})
            schedule['history'] = schedule['history'][-100:]
            if spec['kind'] == 'cron':
                schedule['next_at'] = next_time(spec['cron'], schedule['timezone'], now)
            else:
                schedule['enabled'] = False
            self.store.put(db, 'schedule', schedule)

    def compare(self, ids):
        if not 2 <= len(ids) <= 6:
            raise ValueError('Select 2–6 executions')
        from .workshop import reconcile
        reconcile(self.store)
        if len(set(ids))!=len(ids):raise ValueError('Select distinct executions')
        runs = [self.get('run', id) for id in ids]
        if len({r.get('project_id') for r in runs})!=1:raise ValueError('Compare executions within one project')
        if len({r['category'] for r in runs}) != 1:
            raise ValueError('Compare executions within one category')
        conditions = [r.get('frozen', {}).get('evaluation_policy') for r in runs]
        datasets = [r.get('frozen', {}).get('datasets') for r in runs]
        warnings = []
        if any(not x for x in conditions) or any(x != conditions[0] for x in conditions[1:]):
            warnings.append('Evaluation conditions differ or are unknown.')
        if any(not x for x in datasets) or any(x != datasets[0] for x in datasets[1:]):
            warnings.append('Dataset versions differ or are unknown.')
        def input_size(run):
            config=run.get('frozen',{}).get('config',{})
            data=config.get('data')
            value=config.get('imgsz',config.get('evaluation.imgsz',data.get('target_size') if isinstance(data,dict) else None))
            return str(value) if value is not None else None
        resolution=[input_size(r) for r in runs]
        if any(v is None for v in resolution) or any(v!=resolution[0] for v in resolution[1:]):
            warnings.append('Input resolutions differ or are unknown.')
        def flat(value, prefix=''):
            result = {}
            for k, v in value.items():
                key = f'{prefix}.{k}' if prefix else k
                if isinstance(v, dict):
                    result.update(flat(v,key))
                else:
                    result[key] = v
            return result
        configs = [flat(r.get('frozen',{}).get('config',{})) for r in runs]
        differences = {k:[c.get(k) for c in configs] for k in sorted(set().union(*(c.keys() for c in configs)))
                       if len({json.dumps(c.get(k),sort_keys=True) for c in configs}) > 1}
        from .analytics import summary
        definitions=[d for d in self.list('metric_definition',limit=100000)['items'] if d['project_id']==runs[0].get('project_id')]
        summaries={r['id']:{d['key']:summary(r['metrics'].get(d['key'],[]),d) for d in definitions} for r in runs}
        return {'runs': runs, 'warnings': warnings, 'differences': differences,
                'metric_definitions':definitions,'summaries':summaries}
