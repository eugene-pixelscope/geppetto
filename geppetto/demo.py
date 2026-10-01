"""Explicitly synthetic fixtures and lifecycle; no model, container or GPU execution."""
import json
import math
from pathlib import Path
import time

from PIL import Image, ImageDraw

from .configuration import digest
from .files import fingerprint, manifest
from .service import Service


def seed(service, home):
    home = Path(home)
    with service.store.connect() as db:
        if service.store.all(db, 'dataset'):
            return
        for category, label in [('ball-detection','Ball detection'), ('jersey-recognition','Jersey recognition')]:
            root = home / 'demo-data' / category
            root.mkdir(parents=True, exist_ok=True)
            samples = []
            for i in range(6):
                image = Image.new('RGB', (480,280), '#174c3b')
                draw = ImageDraw.Draw(image)
                for x in range(0,480,80):
                    draw.rectangle((x,0,x+40,280), fill='#1c5944')
                draw.line((0,140,480,140), fill='#8db19d',width=2)
                if category == 'ball-detection':
                    draw.ellipse((190+i*8,105,222+i*8,137), fill='white')
                    draw.rectangle((184+i*8,99,228+i*8,143),outline='#f5d67b',width=2)
                    annotation = {'boxes': [[184+i*8,99,228+i*8,143]], 'class': 'ball'}
                else:
                    draw.rectangle((175,50,305,225), fill='#ebdbb5')
                    draw.text((219,115),str(7+i),fill='#173f38',font_size=44)
                    annotation = {'number': 7+i}
                draw.text((12,12),'SYNTHETIC DEMO',fill='white')
                image.save(root / f'{i}.png')
                (root / f'{i}.json').write_text(json.dumps(annotation))
                samples.append({'image':f'{i}.png','label':f'{i}.json','split':'train' if i < 4 else 'validation'})
            data = manifest(root,samples)
            item = {'id': 'demo-'+category, 'name': label+' · Example data', 'category':category,
                    'root':str(root), 'version':digest(data), 'samples':data,
                    'policy':{'unit':'image','preprocessing':'synthetic','threshold':0.5,'masking':'none'},
                    'splits':{'train':4,'validation':2,'test':0},'created_at':time.time(),
                    'synthetic':True,'status':'registered','effective_samples':6}
            service.store.put(db,'dataset',item)
    for template_id, category in [('ball-p2-v1','ball-detection'),('jersey-vit-v1','jersey-recognition')]:
        for i in range(2):
            body = {'name':f'{"P2 baseline" if category == "ball-detection" else "Digit-aware"} · Experiment {i+1}',
                    'template_id':template_id,'dataset_ids':['demo-'+category],
                    'overrides':{'epochs':12+i*4} if category == 'ball-detection' else {'training':{'max_epochs':8+i*2}}}
            body['confirmation_hash'] = service.preview(body)['confirmation_hash']
            run = service.submit(body,f'demo-{template_id}-{i}')
            run.update({'state':'succeeded','phase':'Completed','started_at':time.time()-800,'finished_at':time.time()-20,
                        'observed_at':time.time(),'last_success_at':time.time(),'demo_tick':12})
            for n in range(1,total_epochs(run)+1):
                add_metrics(run,n,offset=i)
            complete_artifacts(run,home)
            with service.store.connect() as db:
                service.store.put(db,'run',run)


def total_epochs(run):
    config=run['frozen']['config']
    return config.get('epochs',config.get('training',{}).get('max_epochs',12))


def add_metrics(run, step, offset=0):
    template = run['frozen']['template']
    for metric in template['metrics']:
        if metric['direction'] == 'minimize':
            value = 1.5 * math.exp(-step/5) + 0.08 + offset*0.03
        else:
            value = (0.28 + 0.56*(1-math.exp(-step/5)) + offset*0.015)
            if metric['unit'] == 'percent':
                value *= 100
        run['metrics'].setdefault(metric['key'],[]).append({'step':step,'epoch':step,'elapsed':step*60,
                                                        'timestamp':time.time(),'value':round(value,5)})
    for key,value in [('resources/gpu_utilization',45+step%5*8),('resources/gpu_memory_gib',12+step/10),('resources/cpu_percent',60+step%3*6)]:
        run['metrics'].setdefault(key,[]).append({'step':step,'epoch':step,'elapsed':step*60,'timestamp':time.time(),'value':value})
    run['progress'] = {'epoch':step,'step':step,'total':total_epochs(run)}
    run['logs'].append(f'[Demo] epoch {step}/{total_epochs(run)} · Recording loss and validation metrics')


def complete_artifacts(run, home):
    root = Path(home) / 'results' / run['id']
    root.mkdir(parents=True,exist_ok=True)
    (root/'config.json').write_text(json.dumps(run['frozen']['config'],indent=2))
    (root/'evaluation.json').write_text(json.dumps({'synthetic':True,'metrics':run['metrics']},indent=2))
    (root/'weights.demo.txt').write_text('Synthetic fixture. This is not model weights.\n')
    image = Image.new('RGB',(360,320),'#fbf7ec')
    draw = ImageDraw.Draw(image)
    draw.text((15,12),'SYNTHETIC CONFUSION MATRIX',fill='#183d35')
    for i in range(3):
        for j in range(3):
            draw.rectangle((20+j*100,50+i*80,115+j*100,125+i*80), fill='#408777' if i == j else '#dae6de')
            draw.text((60+j*100,80+i*80),str(30 if i == j else 2),fill='white' if i == j else '#183d35')
    image.save(root/'confusion.png')
    run['artifacts'] = [{**fingerprint(root,p),'id':str(i),'root':str(root),'complete':True,'resume':False,'role':role}
                        for i,(p,role) in enumerate([('config.json','configuration'),('evaluation.json','evaluation'),
                                                    ('weights.demo.txt','demo_placeholder'),('confusion.png','evaluation_image')])]


def tick(service, home, now=None):
    now = now or time.time()
    with service.store.connect() as db:
        service.tick_schedules(db,now)
        runs = sorted(service.store.all(db,'run'),key=lambda r:r['created_at'])
        active = [r for r in runs if r['state'] == 'running']
        for run in runs:
            if run.get('frozen',{}).get('execution',{}).get('backend')=='agent':
                continue
            if run['state'] in ('succeeded','failed','stopped','skipped') or run.get('imported'):
                continue
            if run['stop_requested_at']:
                run.update({'state':'stopped','phase':'Stopped','finished_at':now,'resource_released':True})
                run['logs'].append('[Demo] Stop request applied · Simulated resources released')
                if run in active:
                    active.remove(run)
            elif run['state'] == 'queued':
                from .sweeps import can_dispatch
                if not can_dispatch(service.store,db,run,runs):
                    continue
                requested = run['frozen']['gpu'].get('uuid','GPU-DEMO-0')
                occupied = {r.get('allocated_gpu') for r in active}
                if len(active) >= service.concurrency or requested in occupied:
                    continue
                try:
                    from .files import verify_manifest
                    for item in run['frozen']['datasets']:
                        verify_manifest(service.store.get(db,'dataset',item['id']))
                    run.update({'state':'running','phase':'Training','started_at':now,'allocated_gpu':requested,'demo_tick':0})
                    active.append(run)
                except (ValueError,OSError) as exc:
                    run.update({'state':'failed','phase':'Data change detected','finished_at':now})
                    run['logs'].append(str(exc))
            else:
                run['demo_tick'] += 1
                add_metrics(run,run['demo_tick'])
                run['phase'] = 'Validation' if run['demo_tick'] % 3 == 0 else 'Training'
                if run['demo_tick'] >= total_epochs(run):
                    run.update({'state':'succeeded','phase':'Completed','finished_at':now,'resource_released':True})
                    complete_artifacts(run,home)
                    active.remove(run)
            run['observed_at'] = run['last_success_at'] = now
            service.store.put(db,'run',run)
