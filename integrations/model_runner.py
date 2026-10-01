"""Container-only model bridge. Never launched by the web process.

Use inside a separately built, pinned model image after isolated validation.
The source repository stays read-only. Model-specific imports happen only here.
"""
import argparse
import functools
import hashlib
import importlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import signal
import shutil
import sys
import tempfile
import resource
import subprocess
import time


def atomic_json(path,value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,allow_nan=False))
    tmp.replace(path)


class Recorder:
    def __init__(self,root,mlflow,submission,model_name=None):
        self.root,self.mlflow,self.submission=Path(root),mlflow,submission
        self.started=time.time()
        self.metrics={}
        self.model_name=model_name
        self.last_resources=0
        self.metric_names={}

    def record(self,metrics,step,epoch=None,phase='training'):
        clean={key:float(value) for key,value in metrics.items() if isinstance(value,(int,float)) and math.isfinite(float(value))}
        now=time.time()
        if now-self.last_resources>=5:
            self.last_resources=now
            usage=resource.getrusage(resource.RUSAGE_SELF)
            clean['resources/process_peak_memory_mib']=usage.ru_maxrss/1024
            clean['resources/process_cpu_seconds']=usage.ru_utime+usage.ru_stime
            try:
                import torch
                uuid=str(torch.cuda.get_device_properties(0).uuid)
                if not uuid.startswith('GPU-'):uuid='GPU-'+uuid
                measured=subprocess.check_output(['nvidia-smi','-i',uuid,'--query-gpu=utilization.gpu,memory.used',
                    '--format=csv,noheader,nounits'],timeout=3,text=True).strip().split(',')
                clean['resources/gpu_utilization']=float(measured[0])
                clean['resources/gpu_memory_mib']=float(measured[1])
            except (ImportError,AttributeError,RuntimeError,ValueError,OSError,subprocess.SubprocessError):
                # Missing measurements remain missing; never synthesize values.
                pass
        translated={}
        for key,value in clean.items():
            safe=re.sub(r'[^a-zA-Z0-9_./ :\-]','_',key)
            if safe!=key:safe=safe[:180]+'_'+hashlib.sha256(key.encode()).hexdigest()[:12]
            if safe in translated:raise ValueError('MLflow metric name collision')
            self.metric_names[key]=safe;translated[safe]=value
        self.mlflow.log_metrics(translated,step=int(step))
        atomic_json(self.root/'metric-names.json',self.metric_names)
        for key,value in clean.items():
            self.metrics.setdefault(key,[]).append({'step':int(step),'epoch':epoch,'elapsed':now-self.started,'timestamp':now,'value':value})
        atomic_json(self.root/'progress.json',{'submission_id':self.submission,'phase':phase,'step':step,'epoch':epoch,
                                             'observed_at':now,'metrics':self.metrics})

    def finish(self,state,run_id):
        artifacts=[]
        for path in sorted(self.root.rglob('*')):
            if not path.is_file() or path.name.endswith('.tmp') or path.name in ('result.json','progress.json'):
                continue
            if any(part.startswith('.partial') for part in path.parts):
                continue
            # Only publish checkpoints copied after a completed save callback.
            if path.suffix in ('.pt','.pth','.safetensors') and 'completed' not in path.relative_to(self.root).parts:
                continue
            if path.is_symlink():
                raise ValueError('Result symlink is not allowed')
            relative=path.relative_to(self.root).as_posix()
            with path.open('rb') as stream:
                sha=hashlib.file_digest(stream,'sha256').hexdigest()
            role='resume_checkpoint' if path.name=='resume.pt' else 'inference_weights' if path.suffix in ('.pt','.pth','.safetensors') else 'evaluation_image' if path.suffix in ('.png','.jpg') else 'configuration' if path.suffix in ('.yaml','.yml') else 'evaluation'
            artifacts.append({'id':str(len(artifacts)),'path':relative,'size':path.stat().st_size,'sha256':sha,
                              'role':role,'complete':True,'resume':path.name=='resume.pt' and state in ('failed','stopped') and getattr(self,'resume_epoch',-1)+1<getattr(self,'resume_total',0)})
            self.mlflow.log_artifact(str(path),artifact_path=path.parent.relative_to(self.root).as_posix() if path.parent!=self.root else None)
        version=None
        if state=='succeeded' and self.model_name:
            best=next((a for a in artifacts if Path(a['path']).name in ('best.pt','best_checkpoint.pt')),None)
            if not best:
                raise RuntimeError('No finalized inference weights for model registration')
            client=self.mlflow.MlflowClient()
            try:
                client.create_registered_model(self.model_name)
            except self.mlflow.exceptions.MlflowException as exc:
                if exc.error_code!='RESOURCE_ALREADY_EXISTS': raise
            created=client.create_model_version(name=self.model_name,source=self.mlflow.get_artifact_uri(best['path']),run_id=run_id)
            version={'name':self.model_name,'version':str(created.version),'run_id':run_id}
        atomic_json(self.root/'result.json',{'submission_id':self.submission,'state':state,'mlflow_run_id':run_id,
                                           'model_version':version,'metrics':self.metrics,'artifacts':artifacts,'finished_at':time.time()})


def finalize_checkpoint(source,output):
    target=Path(output)/'completed'/Path(source).name
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_suffix(target.suffix+'.tmp')
    shutil.copyfile(source,temporary)
    temporary.replace(target)


def verify_code(root,files):
    for name,expected in files.items():
        path=(root/name).resolve(strict=True)
        if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise ValueError('Pinned model source changed: '+name)


def ball_evaluate(config,root,output,recorder,dataset,policy):
    """Re-evaluate a finalized checkpoint without starting a training loop."""
    import yaml
    sys.path[:0]=[str(root),str(root/'src'),str(root/'ultralytics-src')]
    from ultralytics import YOLO,settings
    settings.update({'mlflow':False,'wandb':False})
    checkpoint=Path(recorder.mlflow.artifacts.download_artifacts(artifact_uri=policy['checkpoint_uri'],dst_path=str(output/'checkpoint')))
    with checkpoint.open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=policy['checkpoint_sha256']:
            raise ValueError('Evaluation checkpoint changed')
    selected=[s for s in dataset['samples'] if s['split']==policy['split']]
    if not selected:raise ValueError('Evaluation split is empty')
    # Same source mapping as the ball training loader.
    for sample in selected:
        parts=Path(sample['image']).parts
        if not parts or parts[0]!='images' or sample.get('label')!=str(Path('labels',*parts[1:]).with_suffix('.txt')):
            raise ValueError('Evaluation labels do not match the YOLO loader mapping')
    splitfile=output/'evaluation-samples.txt'
    splitfile.write_text('\n'.join(str(Path(dataset['root'])/s['image']) for s in selected)+'\n')
    datafile=output/'evaluation-data.yaml'
    datafile.write_text(yaml.safe_dump({'path':'/','train':str(splitfile),'val':str(splitfile),'test':str(splitfile),
                                      'names':{0:'ball'},'kpt_shape':[1,3]}))
    model=YOLO(str(checkpoint))
    result=model.val(data=str(datafile),split='test' if policy['split']=='test' else 'val',
        imgsz=config['imgsz'],batch=config['batch'],device='0',workers=4,conf=policy['confidence'],iou=policy['iou'],
        max_det=policy['max_det'],half=False,rect=True,plots=True,seed=0,deterministic=True,
        project=str(output),name='evaluation')
    values={'evaluation/'+k:float(v) for k,v in result.results_dict.items()}
    values.update({'speed_ms/'+k:float(v) for k,v in result.speed.items()})
    recorder.record(values,step=0,phase='validation')
    atomic_json(output/'evaluation-summary.json',{'policy':policy,'dataset_version':dataset['version'],
                                                 'sample_count':len(selected),'metrics':values})


def same_training_state(expected, actual):
    import torch
    if isinstance(expected,torch.Tensor):
        return isinstance(actual,torch.Tensor) and torch.equal(expected.detach().cpu(),actual.detach().cpu())
    if isinstance(expected,dict):
        return isinstance(actual,dict) and expected.keys()==actual.keys() and all(same_training_state(v,actual[k]) for k,v in expected.items())
    if isinstance(expected,(tuple,list)):
        return isinstance(actual,(tuple,list)) and len(expected)==len(actual) and all(same_training_state(a,b) for a,b in zip(expected,actual))
    return expected==actual


def ball(config,root,output,recorder,dataset,resume=None):
    import yaml
    count = int(os.environ.get('GEPPETTO_GPU_COUNT', '1'))
    rank = int(os.environ.get('RANK', '-1'))
    primary = rank in (-1, 0)
    nnodes = int(os.environ.get('GEPPETTO_NNODES', '1'))
    if count * nnodes > 1 and 'LOCAL_RANK' not in os.environ:
        payload = {'config': config, 'root': str(root), 'output': str(output), 'dataset': dataset,
                   'resume': resume, 'submission_id': recorder.submission, 'input_weights': os.environ.get('GEPPETTO_INPUT_WEIGHTS'),
                   'mlflow_run_id': recorder.mlflow.active_run().info.run_id}
        path = output/'distributed-input.json'
        atomic_json(path, payload)
        rendezvous = ['--standalone', '--nnodes=1'] if nnodes == 1 else [
            '--nnodes='+str(nnodes), '--node-rank=0', '--rdzv-backend=c10d',
            '--rdzv-endpoint='+os.environ['GEPPETTO_MASTER'], '--rdzv-id='+os.environ['GEPPETTO_GROUP'],
            '--rdzv-conf=join_timeout=300,read_timeout=60']
        child = subprocess.Popen([sys.executable, '-m', 'torch.distributed.run', *rendezvous,
            '--nproc-per-node='+str(count), str(Path(__file__).with_name('distributed_ball.py')), str(path)], start_new_session=True)
        try:
            if child.wait() != 0:
                raise RuntimeError('Distributed training worker failed; inspect rank logs')
        except BaseException:
            import signal
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try: child.wait(timeout=90)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL); child.wait()
            raise
        finally:
            if (output/'progress.json').is_file():
                recorder.metrics = json.loads((output/'progress.json').read_text()).get('metrics', {})
            if (output/'distributed-result.json').is_file():
                result = json.loads((output/'distributed-result.json').read_text())
                recorder.resume_epoch = result['resume_epoch']; recorder.resume_total = result['resume_total']
        return
    sys.path[:0]=[str(root),str(root/'src'),str(root/'ultralytics-src')]
    from train import resolve_runtime,validate_training_config
    from ultralytics import YOLO,settings
    variant = config.pop('stem_variant', None)
    variants = {
        'baseline': 'yolo11x-p2p3-pose.yaml',
        'two_stream_d1': 'yolo11x-p2p3-twostream-d1-pose.yaml',
        'two_stream_d3': 'yolo11x-p2p3-twostream-d3-pose.yaml',
    }
    if variant is None:
        source=root/'configs/train/yolo26_p2_pose_baseline_split.yaml'
    else:
        if variant not in variants:
            raise ValueError('Unknown stem_variant: '+str(variant))
        source=root/'configs/train/yolo11x_p2p3_twostream_experiment.yaml'
    cfg=yaml.safe_load(source.read_text())
    validate_training_config(cfg,source)
    # Never invoke the repository's idle-GPU chooser inside a scheduled Pod.
    cfg.update(device='0',scale_batch_by_gpus=False)
    cfg=resolve_runtime(cfg,source.parent)
    cfg.update(config)
    if variant is not None:
        cfg['model']=str(root/'configs/models'/variants[variant])
        cfg['pretrained']=str(root/'yolo11x-pose.pt')
    cfg.update(device=','.join(map(str, range(count))),workers=4,cache=False,project=str(output),name='train',exist_ok=True,save=True,val=True,plots=True)
    for key in ('device_count','scale_batch_by_gpus','reference_gpus','min_batch','max_memory_used_mb','max_utilization_percent','min_free_mb','allow_cpu','notes','wandb'):
        cfg.pop(key,None)
    data={'path':'/','names':{0:'ball'},'kpt_shape':[1,3]}
    for source_split,target in [('train','train'),('validation','val'),('test','test')]:
        rows=[str(Path(dataset['root'])/s['image']) for s in dataset['samples'] if s['split']==source_split]
        if rows:
            file=output/(source_split+'.txt')
            if primary: file.write_text('\n'.join(rows)+'\n')
            data[target]=str(file)
    if 'train' not in data or 'val' not in data:
        raise ValueError('Ball training requires explicit train and validation samples')
    for sample in dataset['samples']:
        # Ultralytics resolves labels from images/... to labels/....txt.
        expected=str(Path(sample['image']).with_suffix('.txt')).replace('/images/','/labels/')
        if expected.startswith('images/'):
            expected='labels/'+expected[len('images/'):]
        if sample.get('label')!=expected:
            raise ValueError('Registered label does not match the Ultralytics image-to-label mapping')
    datafile=output/'dataset.yaml'
    if primary: datafile.write_text(yaml.safe_dump(data))
    else:
        deadline = time.monotonic() + 120
        while not datafile.is_file():
            if time.monotonic() > deadline: raise TimeoutError('Rank zero dataset setup timed out')
            time.sleep(.1)
    cfg['data']=str(datafile)
    pretrained=cfg.get('pretrained')
    if isinstance(pretrained,str) and (root/pretrained).is_file():
        cfg['pretrained']=str(root/pretrained)
    settings.update({'mlflow':False,'wandb':False})
    if primary: (output/'effective-config.yaml').write_text(yaml.safe_dump(cfg))
    architecture = cfg.pop('model')
    model=YOLO(os.environ.get('GEPPETTO_INPUT_WEIGHTS') or architecture)
    if resume:
        import torch
        checkpoint=Path(recorder.mlflow.artifacts.download_artifacts(artifact_uri=resume['checkpoint_uri'],dst_path=str(output/('checkpoint-rank-'+str(rank)))))
        if hashlib.sha256(checkpoint.read_bytes()).hexdigest()!=resume['checkpoint_sha256']:
            raise ValueError('Resume checkpoint changed')
        # This pickle comes exclusively from a hashed, managed image checkpoint.
        state=torch.load(checkpoint,map_location='cpu',weights_only=False)
        if state.get('format')!='geppetto-ball-epoch-v1' or state['epoch']+1>=config['epochs']:
            raise ValueError('No unfinished epochs in this managed checkpoint')
        best=None
        if resume.get('best_uri'):
            best=Path(recorder.mlflow.artifacts.download_artifacts(artifact_uri=resume['best_uri'],dst_path=str(output/('checkpoint-rank-'+str(rank)))))
            if hashlib.sha256(best.read_bytes()).hexdigest()!=resume['best_sha256']:raise ValueError('Best checkpoint changed')
        def restore(trainer):
            import random
            import numpy as np
            target_model = trainer.model.module if hasattr(trainer.model, 'module') else trainer.model
            target_model.load_state_dict(state['model'])
            trainer.optimizer.load_state_dict(state['optimizer'])
            trainer.scaler.load_state_dict(state['scaler'])
            trainer.scheduler.load_state_dict(state['scheduler'])
            checks={key:same_training_state(state[key],(target_model if key == 'model' else getattr(trainer,key)).state_dict()) for key in ('model','optimizer','scheduler','scaler')}
            if not all(checks.values()):raise ValueError('Restored training state differs from checkpoint: '+str(checks))
            if trainer.ema:
                trainer.ema.ema.load_state_dict(state['ema'])
                trainer.ema.updates=state['updates']
            trainer.best_fitness=state['best_fitness']
            trainer.stopper.__dict__.update(state['stopper'])
            trainer.start_epoch=state['epoch']+1
            if primary and state.get('best_weights'):
                trainer.best.parent.mkdir(parents=True,exist_ok=True);trainer.best.write_bytes(state['best_weights'])
            elif primary and best:
                trainer.best.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(best,trainer.best)
            rng = state
            if rank >= 0:
                ranks = state.get('rank_rng', [])
                if len(ranks) != int(os.environ['WORLD_SIZE']):
                    raise ValueError('Resume requires per-rank RNG state for the same world size')
                rng = ranks[rank]
            random.setstate(rng['random']);np.random.set_state(rng['numpy'])
            torch.set_rng_state(rng['torch']);torch.cuda.set_rng_state_all(rng['cuda'])
            if trainer.start_epoch>trainer.epochs-trainer.args.close_mosaic:
                trainer._close_dataloader_mosaic();trainer.train_loader.reset()
            atomic_json(output/('resume-verification.json' if primary else 'resume-verification-rank-'+str(rank)+'.json'),{'parent_id':resume['parent_id'],
                'restored_state_matches':checks,'start_epoch':trainer.start_epoch,'optimizer_states':len(trainer.optimizer.state),
                'scheduler_epoch':trainer.scheduler.last_epoch,'ema_updates':trainer.ema.updates if trainer.ema else None,
                'semantics':'epoch boundary; prefetched data-loader batches are rebuilt'})
        model.add_callback('on_pretrain_routine_end',restore)
    def epoch(trainer):
        if not primary: return
        values={**trainer.metrics,**trainer.label_loss_items(trainer.tloss,prefix='train')}
        recorder.record(values,trainer.epoch,trainer.epoch+1)
    model.add_callback('on_fit_epoch_end',epoch)
    rank_rng = []
    if rank >= 0:
        def collect_rng(trainer):
            import torch, random, numpy as np
            local = {'random':random.getstate(), 'numpy':np.random.get_state(),
                     'torch':torch.get_rng_state(), 'cuda':torch.cuda.get_rng_state_all()}
            gathered = [None] * int(os.environ['WORLD_SIZE'])
            torch.distributed.all_gather_object(gathered, local)
            rank_rng[:] = gathered
        model.add_callback('on_train_epoch_end', collect_rng)
    def saved(trainer):
        if not primary: return
        for path in (trainer.best,trainer.last):
            if Path(path).is_file(): finalize_checkpoint(path,output)
        import torch,random,numpy as np
        target=output/'completed'/'resume.pt'
        temporary=target.with_suffix('.tmp')
        torch.save({'format':'geppetto-ball-epoch-v1','epoch':trainer.epoch,
            'model':(trainer.model.module if hasattr(trainer.model,'module') else trainer.model).state_dict(),'optimizer':trainer.optimizer.state_dict(),
            'scaler':trainer.scaler.state_dict(),'scheduler':trainer.scheduler.state_dict(),
            'ema':trainer.ema.ema.state_dict(),'updates':trainer.ema.updates,'best_fitness':trainer.best_fitness,
            'stopper':trainer.stopper.__dict__,'best_weights':Path(trainer.best).read_bytes() if Path(trainer.best).is_file() else None,'random':random.getstate(),'numpy':np.random.get_state(),
            'torch':torch.get_rng_state(),'cuda':torch.cuda.get_rng_state_all(),'rank_rng':rank_rng},temporary)
        temporary.replace(target)
        recorder.resume_epoch=trainer.epoch
        recorder.resume_total=config['epochs']
        if os.environ.get('GEPPETTO_PREEMPTIBLE') == '1' and trainer.epoch + 1 < config['epochs']:
            remote_path = 'checkpoints/epoch-' + str(trainer.epoch)
            recorder.mlflow.log_artifact(str(target), artifact_path=remote_path)
            checksum = hashlib.sha256(target.read_bytes()).hexdigest()
            atomic_json(output/'checkpoint.json', {'submission_id': recorder.submission, 'live_checkpoint': {
                'id': 'live-resume', 'path': remote_path+'/resume.pt', 'sha256': checksum,
                'size': target.stat().st_size, 'role': 'resume_checkpoint', 'resume': True, 'complete': True,
                'artifact_uri': recorder.mlflow.get_artifact_uri()}})
        if count * nnodes > 1:
            atomic_json(output/'distributed-result.json', {'resume_epoch': trainer.epoch, 'resume_total': config['epochs']})
    model.add_callback('on_model_save',saved)
    if rank >= 0:
        # The pinned trainer computes local size from device; use torchrun's global size before setup.
        base_trainer = model._smart_load('trainer')
        class DistributedTrainer(base_trainer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.world_size = int(os.environ['WORLD_SIZE'])
        model.train(trainer=DistributedTrainer, **cfg)
    else:
        model.train(**cfg)
    if primary and not Path(model.trainer.best).is_file():
        raise RuntimeError('Training completed without inference weights')


def jersey(config,root,output,recorder,dataset):
    import yaml
    sys.path[:0]=[str(root),str(root/'src')]
    os.environ['OCR_DIR']=str(root)
    model=importlib.import_module('train')
    from config import Config
    import torch
    if torch.cuda.device_count()!=1:
        raise ValueError('Jersey bridge is validated only for a single visible allocated device')
    parsed=Config.model_validate(config)
    if parsed.model_dump(mode='json')!=config:
        raise ValueError('Runtime Config changed frozen values')
    if str(parsed.data.root_dir)!=dataset['root']:
        raise ValueError('Dataset mount differs from frozen Config path')
    if parsed.wandb and parsed.wandb.enabled:
        raise ValueError('This adapter records to MLflow; W&B must be explicitly disabled')
    if parsed.data.demote_keys_file:
        registered_sources={s['path'] for s in dataset.get('source_files',[])}
        cleaning=Path(parsed.data.demote_keys_file).resolve().relative_to(dataset['root']).as_posix()
        if cleaning not in registered_sources:
            raise ValueError('Label-cleaning policy file is not registered')
    original_validate=model.validate_step
    @functools.wraps(original_validate)
    def validate(*args,**kwargs):
        bound=inspect.signature(original_validate).bind(*args,**kwargs)
        result=original_validate(*args,**kwargs)
        path=Path(bound.arguments['run_dir'])
        recorder.record({'val/'+path.parent.name+'/'+k:v for k,v in result.items()},
                        bound.arguments['global_step'],phase='validation')
        return result
    model.validate_step=validate
    original_save=model.save_checkpoint
    @functools.wraps(original_save)
    def checkpoint(*args,**kwargs):
        # Upstream torch.save is not atomic. Save to staging, then replace files.
        bound=inspect.signature(original_save).bind(*args,**kwargs)
        real_run=Path(bound.arguments['run_dir'])
        real_step=bound.arguments.get('step_path')
        with tempfile.TemporaryDirectory(prefix='.partial-',dir=output) as staging:
            bound.arguments['run_dir']=Path(staging)
            if real_step is not None: bound.arguments['step_path']=Path(staging)/Path(real_step).name
            original_save(*bound.args,**bound.kwargs)
            for file in Path(staging).glob('*.pt'):
                target=Path(real_step) if real_step and file.name==Path(real_step).name else real_run/file.name
                target.parent.mkdir(parents=True,exist_ok=True)
                file.replace(target)
                finalize_checkpoint(target,output)
    model.save_checkpoint=checkpoint
    original_train=model.train_and_evaluate
    @functools.wraps(original_train)
    def train(*args,**kwargs):
        bound=inspect.signature(original_train).bind(*args,**kwargs)
        logger=bound.arguments.get('logger')
        class Logger:
            def __bool__(self): return bool(logger)
            def __getattr__(self,key): return getattr(logger,key)
            def info(self,message,*values,**options):
                text=message % values if values else message
                match=re.search(r'Epoch (\d+), Step (\d+), Loss: ([\d.eE+-]+)',text)
                if match:
                    metrics={'train/loss':float(match[3])}
                    for label,key in [('Acc','train/top1_acc'),('LR','train/learning_rate'),('Digit Aux Loss','train/digit_aux_loss')]:
                        value=re.search(re.escape(label)+r': ([\d.eE+-]+)',text)
                        if value: metrics[key]=float(value[1])
                    recorder.record(metrics,int(match[2]),int(match[1]))
                return logger.info(message,*values,**options)
        bound.arguments['logger']=Logger()
        return original_train(*bound.args,**bound.kwargs)
    model.train_and_evaluate=train
    original_create=model.create_datasets
    def create(*args,**kwargs):
        train_data,validation=original_create(*args,**kwargs)
        registered={s['image']:s['split'] for s in dataset['samples']}
        label_files={s['label_file']['path'] for s in dataset['samples'] if s.get('label_file')}
        label_files.update(s['path'] for s in dataset.get('source_files',[]))
        def save(ds,label,split):
            if hasattr(ds,'datasets'):
                for i,child in enumerate(ds.datasets): save(child,label+'-'+str(i),split)
            elif hasattr(ds,'detection_df'):
                if not len(ds): raise ValueError('No effective samples after filtering')
                for match in ds.match_data.values():
                    for key in ('track_file','jersey_gt_file','jersey_pred_file'):
                        file=getattr(match,key,None)
                        if file and Path(file).relative_to(dataset['root']).as_posix() not in label_files:
                            raise ValueError('Loader uses an unregistered label/tracking source: '+str(file))
                for row in ds.detection_df.itertuples():
                    candidates=ds._get_possible_img_paths(row.match_key,int(row.file_name),int(row.detection_id))
                    file=next((p for p in candidates if p.is_file()),None)
                    if file is None or registered.get(file.relative_to(dataset['root']).as_posix())!=split:
                        raise ValueError('Loader sample is absent from the registered split')
                path=output/('effective-samples-'+label+'.csv')
                ds.detection_df.to_csv(path,index=True)
            else:
                raise ValueError('Cannot record effective sample identities for this dataset')
        save(train_data,'train','train')
        for name,ds in validation.items(): save(ds,'validation-'+name,'validation')
        return train_data,validation
    model.create_datasets=create
    run_dir=parsed.logging.run_dir
    run_dir.mkdir(parents=True,exist_ok=False)
    (run_dir/'resolved.yaml').write_text(yaml.safe_dump(config))
    model.train_worker(0,1,parsed,run_dir,parsed.data.val_dataset_names)
    if not (run_dir/'best_checkpoint.pt').is_file():
        raise RuntimeError('No completed best checkpoint; interrupted/empty training is not success')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--spec',type=Path,required=True)
    parser.add_argument('--model-root',type=Path,required=True)
    parser.add_argument('--dataset-registry',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=Path('/outputs'))
    args=parser.parse_args()
    spec=json.loads(args.spec.read_text())
    frozen=spec['frozen']
    root=args.model_root.resolve(strict=True)
    files=frozen['template'].get('code_files')
    if not files: raise ValueError('Model source manifest required')
    verify_code(root,files)
    if frozen['relation'] not in ('train','resume','evaluate') or frozen['relation']=='evaluate' and frozen['template']['adapter']!='ball':
        raise ValueError('This image supports training and ball evaluation')
    from geppetto.configuration import digest
    from geppetto.files import verify_manifest
    from jsonschema import Draft202012Validator
    if digest(frozen['config'])!=frozen['config_sha256']: raise ValueError('Config digest mismatch')
    if digest(frozen['template']['schema'])!=frozen['schema_sha256']: raise ValueError('Schema digest mismatch')
    Draft202012Validator(frozen['template']['schema']).validate(frozen['config'])
    registry=json.loads(args.dataset_registry.read_text())
    if len(frozen['datasets'])!=1: raise ValueError('This image requires one composite dataset version')
    ref=frozen['datasets'][0]
    dataset=registry[ref['id']]
    if dataset['version']!=ref['version']: raise ValueError('Dataset version mismatch')
    if frozen.get('execution',{}).get('backend')=='agent':
        locations=frozen['execution']['locations']
        if len(locations)!=1 or dataset['root']!='/datasets/'+locations[0]['root']:
            raise ValueError('Dataset root differs from the reviewed PVC location')
    if frozen.get('evaluation',{}).get('method')=='gradcam':
        from integrations.gradcam import verify_sample
        verify_sample(dataset,frozen['evaluation'])
    else:
        verify_manifest(dataset)
    args.output.mkdir(parents=True,exist_ok=True)
    atomic_json(args.output/'frozen.json',frozen)
    import torch
    expected = frozen['gpu']['count']
    if torch.cuda.device_count()!=expected:raise ValueError('Visible GPU count differs from the reviewed allocation')
    os.environ['GEPPETTO_GPU_COUNT'] = str(expected)
    os.environ['GEPPETTO_PREEMPTIBLE'] = '1' if frozen.get('preemptible') else '0'
    atomic_json(args.output/'allocation.json',{'visible_devices':torch.cuda.device_count(),
        'gpu_uuid':str(torch.cuda.get_device_properties(0).uuid),'device_name':torch.cuda.get_device_name(0)})
    os.chdir('/cache')
    import mlflow
    mlflow.set_tracking_uri(os.environ['MLFLOW_TRACKING_URI'])
    mlflow.set_experiment('geppetto-'+frozen['template']['category'])
    def stopped(signum,frame): raise SystemExit(143)
    signal.signal(signal.SIGTERM,stopped)
    signal.signal(signal.SIGINT,stopped)
    with mlflow.start_run(run_name=spec['submission_id']) as run:
        mlflow.set_tags({'geppetto.submission_id':spec['submission_id'],'geppetto.template_id':frozen['template']['id'],
                         'geppetto.config_sha256':frozen['config_sha256'],
                         'airflow.run_id':os.environ.get('AIRFLOW_RUN_ID',''),
                         'airflow.dag_id':os.environ.get('AIRFLOW_DAG_ID','')})
        recorder=Recorder(args.output,mlflow,spec['submission_id'],frozen['template']['id'] if frozen['relation'] in ('train','resume') else None)
        def flatten(value,prefix=''):
            values={}
            for key,item in value.items():
                name=prefix+'.'+key if prefix else key
                if isinstance(item,dict): values.update(flatten(item,name))
                else: values[name]=json.dumps(item) if isinstance(item,list) else item
            return values
        mlflow.log_params(flatten(frozen['config']))
        atomic_json(args.output/'tracking.json',{'mlflow_run_id':run.info.run_id,'experiment_id':run.info.experiment_id,
                                                'artifact_uri':mlflow.get_artifact_uri()})
        try:
            if frozen.get('input_weights'):
                weights = frozen['input_weights']
                downloaded = Path(mlflow.artifacts.download_artifacts(artifact_uri=weights['checkpoint_uri'], dst_path=str(args.output/'input')))
                if hashlib.sha256(downloaded.read_bytes()).hexdigest() != weights['checkpoint_sha256']:
                    raise ValueError('Upstream checkpoint hash differs')
                os.environ['GEPPETTO_INPUT_WEIGHTS'] = str(downloaded)
            if frozen['relation']=='evaluate':
                if frozen['evaluation'].get('method')=='gradcam':
                    from integrations.gradcam import run as gradcam
                    gradcam(frozen['config'],root,args.output,recorder,dataset,frozen['evaluation'])
                else:
                    ball_evaluate(frozen['config'],root,args.output,recorder,dataset,frozen['evaluation'])
            elif frozen['relation']=='resume':
                ball(frozen['config'],root,args.output,recorder,dataset,frozen['resume'])
            else:
                {'ball':ball,'jersey':jersey}[frozen['template']['adapter']](frozen['config'],root,args.output,recorder,dataset)
        except BaseException as exc:
            state='stopped' if isinstance(exc,(KeyboardInterrupt,SystemExit)) else 'failed'
            recorder.finish(state,run.info.run_id)
            mlflow.end_run(status='KILLED' if state=='stopped' else 'FAILED')
            raise
        else:
            recorder.finish('succeeded',run.info.run_id)


if __name__=='__main__':
    main()
