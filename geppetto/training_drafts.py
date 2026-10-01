"""Training configuration drafts. Saving never creates a run or submits work."""
import math
import time
import uuid
from .workshop import reconcile

ARCHITECTURES=[
 {'id':'yolo11x-pose','name':'YOLO11x','category':'ball-detection','defaults':{'epochs':100,'batch':8,'imgsz':1920,'patience':100,'learning_rate':.01}},
 {'id':'yolo26x-pose','name':'YOLO26x','category':'ball-detection','defaults':{'epochs':100,'batch':8,'imgsz':1920,'patience':100,'learning_rate':.01}},
 {'id':'yolo26-p2-pose','name':'YOLO26 P2','category':'ball-detection','defaults':{'epochs':245,'batch':16,'imgsz':1920,'patience':100,'learning_rate':.0054}},
 {'id':'yolo26-tinyball','name':'YOLO26 P2 · CBAM + ESMoE_DCN + NWD','category':'ball-detection','defaults':{'epochs':245,'batch':16,'imgsz':1920,'patience':100,'learning_rate':.0054}},
 {'id':'jersey-vit','name':'Jersey ViT','category':'jersey-recognition','defaults':{'epochs':10,'batch':64,'imgsz':224,'learning_rate':.0002}},
 {'id':'jersey-resnet18','name':'Jersey ResNet18 · Multiple datasets','category':'jersey-recognition','defaults':{'epochs':30,'batch':64,'imgsz':128,'learning_rate':.001,'patience':10,'sampling':'balanced-source'}},
]


def options(service):
    reconcile(service.store)
    return {'architectures':ARCHITECTURES,'drafts':service.list('training_draft',limit=100)['items'],
            'execution_ready':False,'execution_note':'Training submission is not connected yet.'}


def validate(service,body):
    if service.mode=='readonly':raise ValueError('Read-only mode')
    reconcile(service.store)
    architecture=next((a for a in ARCHITECTURES if a['id']==body.get('architecture_id')),None)
    if not architecture:raise ValueError('Select an architecture.')
    node=service.get('whale',body.get('node_id',''))
    project=service.get('project',body.get('project_id',''))
    ids=body.get('dataset_ids')
    if ids is None:ids=[body.get('dataset_id','')]
    if not isinstance(ids,list) or not ids or any(not isinstance(id,str) or not id for id in ids) or len(ids)!=len(set(ids)):
        raise ValueError('Select one or more distinct datasets.')
    datasets=[service.get('dataset',id) for id in ids]
    if project['category']!=architecture['category'] or any(d['category']!=architecture['category'] for d in datasets):
        raise ValueError('Project, architecture, and dataset task categories must match.')
    if any(d.get('node_id')!=node['id'] for d in datasets):raise ValueError('Use datasets from the selected environment.')
    versions={d['id']:d['version'] for d in datasets}
    if body.get('dataset_versions') is not None and body['dataset_versions']!=versions:
        raise ValueError('Dataset versions changed. Select the datasets again.')
    name=body.get('name')
    if not isinstance(name,str) or not name.strip() or len(name.strip())>160:raise ValueError('Enter a training name.')
    params=body.get('parameters')
    if not isinstance(params,dict) or set(params)!=set(architecture['defaults']):raise ValueError('Check the training parameters.')
    for key,low,high in [('epochs',1,10000),('batch',1,4096),('imgsz',32,8192),('patience',0,10000)]:
        if key not in params:continue
        if type(params[key]) is not int or not low<=params[key]<=high:raise ValueError(key+' value is out of range.')
    lr=params['learning_rate']
    if type(lr) not in (int,float) or not math.isfinite(lr) or not 0<lr<=1:raise ValueError('Learning rate must be greater than 0 and at most 1.')
    if architecture['category']=='ball-detection' and params['imgsz']%32:raise ValueError('YOLO input size must be a multiple of 32.')
    config=None
    if architecture['id']=='jersey-resnet18':
        if params['batch']<2:raise ValueError('ResNet18 requires batch size of at least 2.')
        if params['sampling'] not in ('balanced-source','proportional'):raise ValueError('Unknown dataset sampling strategy.')
        if any(d.get('policy',{}).get('format')!='jersey-number' or not d.get('local_manifest') or not d.get('content_verified') for d in datasets):
            raise ValueError('Select prepared, content-verified jersey datasets.')
        if any(not sum(d.get('splits',{}).get(split,0) for d in datasets) for split in ('train','validation')):
            raise ValueError('Select both training and validation data. Test data cannot replace validation.')
        if len({d['root'] for d in datasets})!=1:
            raise ValueError('Selected jersey datasets must share the same data root.')
        from .files import iter_samples
        seen=set()
        for dataset in datasets:
            for sample in iter_samples(dataset):
                sha=sample['image_file']['sha256']
                if sha in seen:raise ValueError('Selected datasets contain overlapping images. Prepare a deduplicated version.')
                seen.add(sha)
        config={'schema_version':1,'architecture_id':architecture['id'],
                'manifests':[{'path':d['local_manifest'],'version':d['local_manifest_version']} for d in datasets],
                'dataset_versions':versions,'parameters':params}
    dataset=datasets[0]
    result={'name':name.strip(),'node_id':node['id'],'node_name':node['name'],'project_id':project['id'],
            'architecture_id':architecture['id'],'architecture_name':architecture['name'],
            'category':architecture['category'],'dataset_id':dataset['id'],'dataset_name':dataset['name'],
            'dataset_version':dataset['version'],'dataset_ids':ids,'dataset_names':[d['name'] for d in datasets],
            'dataset_versions':versions,'splits':{s:sum(d.get('splits',{}).get(s,0) for d in datasets) for s in ('train','validation','test')},
            'parameters':params,'state':'draft','execution_ready':False,
            'blockers':['Training submission adapter and model image connection required']+(['Full dataset content validation required'] if any(d.get('content_verified') is False for d in datasets) else [])}
    if config:
        result.update(training_config=config,local_runner=dataset.get('local_runner'),local_training_ready=True)
    return result


def save(service,body):
    item=validate(service,body)
    with service.store.connect() as db:
        id=body.get('id')
        if id:
            old=service.store.get(db,'training_draft',id)
            item.update(id=id,created_at=old['created_at'])
        else:item.update(id=uuid.uuid4().hex,created_at=time.time())
        item['updated_at']=time.time();service.store.put(db,'training_draft',item)
    return item
