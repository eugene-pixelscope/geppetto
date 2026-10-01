"""Single-class detection diagnostics. These thresholded counts are not mAP."""
import math
import time
from collections import defaultdict
from PIL import Image
from .configuration import digest
from .files import open_beneath, fingerprint
from .indexed_datasets import sample_at


def number(value):
    if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value):
        raise ValueError('Expected a finite number')
    return value


def box(value):
    if not isinstance(value,list) or len(value)!=4:
        raise ValueError('box must be normalized [cx, cy, width, height]')
    x,y,w,h=map(number,value)
    if not (0<=x<=1 and 0<=y<=1 and 0<w<=1 and 0<h<=1):
        raise ValueError('Invalid normalized box')
    return [x,y,w,h]


def source_sample(dataset,index):
    sample=sample_at(dataset,index)
    if not sample.get('label'):
        raise ValueError('Missing ground truth label; absence is not a negative image')
    image_hash=fingerprint(dataset['root'],sample['image'])
    label_hash=fingerprint(dataset['root'],sample['label'])
    for role,actual in [('image',image_hash),('label',label_hash)]:
        if sample.get(role+'_file') and sample[role+'_file']!=actual:
            raise ValueError('Registered sample changed')
    with open_beneath(dataset['root'],sample['image']) as stream:
        with Image.open(stream) as image:
            width,height=image.size
    with open_beneath(dataset['root'],sample['label']) as stream:
        text=stream.read().decode('utf-8')
    if dataset.get('synthetic'):
        import json
        gt=[box([(x1+x2)/2/width,(y1+y2)/2/height,(x2-x1)/width,(y2-y1)/height])
            for x1,y1,x2,y2 in json.loads(text)['boxes']]
    elif dataset.get('policy',{}).get('format') in ('yolo','yolo-pose'):
        gt=[]
        for line in text.splitlines():
            if not line.strip():
                continue
            values=[float(v) for v in line.split()]
            if len(values)<5 or any(not math.isfinite(v) for v in values) or values[0]!=0:
                raise ValueError('Expected class 0 ball YOLO labels')
            gt.append(box(values[1:5]))
    else:
        raise ValueError('Error analysis requires YOLO ball labels')
    # Ensure the ground truth read and image geometry belong to the pinned files.
    if image_hash!=fingerprint(dataset['root'],sample['image']) or label_hash!=fingerprint(dataset['root'],sample['label']):
        raise ValueError('Source changed while importing evaluation')
    return {'index':index,'image':sample['image'],'width':width,'height':height,'truth':gt,
            'camera':sample['image'].split('/')[-1].split('___')[0] if '___' in sample['image'] else 'Not recorded',
            'image_file':image_hash,'label_file':label_hash,'split':sample['split']}


def import_evaluation(service,run_id,body):
    run=service.get('run',run_id)
    if run['category']!='ball-detection':
        raise ValueError('Ball detection only')
    dataset=service.get('dataset',body.get('dataset_id',''))
    if dataset['category']!='ball-detection' or body.get('dataset_version')!=dataset['version']:
        raise ValueError('Dataset version mismatch')
    # Evaluation pins its own registered dataset version; imported runs may lack training provenance.
    training_dataset_match=any(d['id']==dataset['id'] and d['version']==dataset['version'] for d in (run.get('frozen',{}).get('datasets') or []))
    split=body.get('split')
    if split not in ('validation','test'):
        raise ValueError('Use validation or test split')
    checkpoint=body.get('checkpoint')
    if not isinstance(checkpoint,str) or not checkpoint.strip() or len(checkpoint)>300:
        raise ValueError('Provide the evaluated checkpoint identifier or SHA-256')
    floor=number(body.get('prediction_floor',0))
    if not 0<=floor<=1:
        raise ValueError('prediction_floor must be between 0 and 1')
    rows=body.get('images')
    if not isinstance(rows,list) or not 1<=len(rows)<=100000:
        raise ValueError('Provide 1–100,000 evaluated images per report')
    seen=set(); records=[]
    for row in rows:
        if not isinstance(row,dict) or type(row.get('index')) is not int or row['index'] in seen:
            raise ValueError('Duplicate or invalid image index')
        seen.add(row['index'])
        predictions=row.get('predictions')
        if not isinstance(predictions,list) or len(predictions)>300:
            raise ValueError('Provide predictions, including [] for evaluated images with no detections')
        cleaned=[]
        for p in predictions:
            if not isinstance(p,dict) or p.get('class_id',0)!=0:
                raise ValueError('Only class 0 ball predictions are supported')
            score=number(p.get('score'))
            if not floor<=score<=1:
                raise ValueError('Prediction score is outside the exported score range')
            cleaned.append({'box':box(p.get('box')),'score':score})
        try:
            record=source_sample(dataset,row['index'])
        except (OSError,UnicodeDecodeError) as exc:
            raise ValueError('Cannot read evaluation image or ground truth label: '+str(row['index'])) from exc
        if record['split']!=split:
            raise ValueError('Sample does not belong to the declared evaluation split')
        records.append({**record,'predictions':cleaned})
    report={'run_id':run_id,'dataset_id':dataset['id'],'dataset_version':dataset['version'],
            'checkpoint':checkpoint,'split':split,'prediction_floor':floor,'records':records,
            'training_dataset_match':training_dataset_match,
            'split_count':dataset['splits'][split], 'synthetic':bool(run.get('synthetic') or dataset.get('synthetic')),
            'provenance':'Imported predictions; checkpoint identity is supplied by the importer, not independently verified.'}
    report['id']=digest(report)[:24]
    report['created_at']=time.time()
    with service.store.connect() as db:
        service.store.put(db,'evaluation',report)
    return {'id':report['id'],'images':len(records)}


def iou(a,b):
    ax,ay,aw,ah=a; bx,by,bw,bh=b
    overlap=max(0,min(ax+aw/2,bx+bw/2)-max(ax-aw/2,bx-bw/2))*max(0,min(ay+ah/2,by+bh/2)-max(ay-ah/2,by-bh/2))
    return overlap/(aw*ah+bw*bh-overlap)


def matches(truth,predictions,threshold):
    """Descending confidence, best available IoU, one-to-one matching."""
    used=set(); matched=set()
    for j,p in sorted(enumerate(predictions),key=lambda item:-item[1]['score']):
        available=[(iou(g,p['box']),i) for i,g in enumerate(truth) if i not in used]
        if available:
            score,i=max(available)
            if score>=threshold:
                used.add(i);matched.add(j)
    return used,matched


def analyze(report,confidence=.5,overlap=.5,camera='',kind='fn',cell=None,offset=0,sort='errors'):
    if not report['prediction_floor']<=confidence<=1 or not 0<overlap<=1:
        raise ValueError('Confidence must be at least the export floor; IoU must be > 0 and <= 1')
    if kind not in ('fn','fp') or cell is not None and not 0<=cell<64 or offset<0:
        raise ValueError('Invalid error filter')
    if sort not in ('errors','index','confidence'):raise ValueError('Invalid failure sorting')
    cameras=sorted({r['camera'] for r in report['records']})
    if camera and camera not in cameras:
        raise ValueError('Unknown camera')
    cells=[{'truth':0,'fn':0,'predictions':0,'fp':0} for _ in range(64)]
    sizes={name:{'truth':0,'tp':0} for name in ('<16 px','16–32 px','32–64 px','≥64 px')}
    groups=defaultdict(lambda:{'truth':0,'tp':0,'fp':0,'images':0})
    totals={'images':0,'truth':0,'predictions':0,'tp':0,'fn':0,'fp':0}
    failures=[]
    heatmap={'width':128,'height':72,**{k:[0]*(128*72) for k in ('truth','fn','predictions','fp')},'background':None}
    def heat_location(b):return min(71,int(b[1]*72))*128+min(127,int(b[0]*128))
    def location(b):return min(7,int(b[1]*8))*8+min(7,int(b[0]*8))
    for row in report['records']:
        if camera and row['camera']!=camera:continue
        if heatmap['background'] is None:heatmap['background']={k:row[k] for k in ('index','image','width','height')}
        gt=row['truth']; pred=[p for p in row['predictions'] if p['score']>=confidence]
        used,matched=matches(gt,pred,overlap)
        fn=[i for i in range(len(gt)) if i not in used];fp=[i for i in range(len(pred)) if i not in matched]
        totals['images']+=1; totals['truth']+=len(gt); totals['predictions']+=len(pred)
        totals['tp']+=len(used);totals['fn']+=len(fn);totals['fp']+=len(fp)
        group=groups[row['camera']];group['images']+=1;group['truth']+=len(gt);group['tp']+=len(used);group['fp']+=len(fp)
        for i,g in enumerate(gt):
            c=cells[location(g)];c['truth']+=1;c['fn']+=int(i not in used)
            j=heat_location(g);heatmap['truth'][j]+=1;heatmap['fn'][j]+=int(i not in used)
            side=math.sqrt(g[2]*row['width']*g[3]*row['height'])
            bucket=sizes['<16 px' if side<16 else '16–32 px' if side<32 else '32–64 px' if side<64 else '≥64 px']
            bucket['truth']+=1;bucket['tp']+=int(i in used)
        for i,p in enumerate(pred):
            c=cells[location(p['box'])];c['predictions']+=1;c['fp']+=int(i not in matched)
            j=heat_location(p['box']);heatmap['predictions'][j]+=1;heatmap['fp'][j]+=int(i not in matched)
        indexes=fn if kind=='fn' else fp
        boxes=gt if kind=='fn' else [p['box'] for p in pred]
        if any(cell is None or location(boxes[i])==cell for i in indexes):
            failures.append({'index':row['index'],'image':row['image'],'camera':row['camera'],'width':row['width'],'height':row['height'],
                'truth':[{'box':g,'missed':i not in used} for i,g in enumerate(gt)],
                'predictions':[{**p,'false_positive':i not in matched} for i,p in enumerate(pred)],'fn':len(fn),'fp':len(fp),
                'selected_errors':sum(cell is None or location(boxes[i])==cell for i in indexes),
                'max_fp_score':max((pred[i]['score'] for i in fp),default=0)})
    if sort=='errors':failures.sort(key=lambda r:(-r['selected_errors'],r['index']))
    elif sort=='confidence':failures.sort(key=lambda r:(-r['max_fp_score'],r['index']))
    else:failures.sort(key=lambda r:r['index'])
    totals['recall']=totals['tp']/totals['truth'] if totals['truth'] else None
    totals['precision']=totals['tp']/totals['predictions'] if totals['predictions'] else None
    for c in cells:
        c['fn_rate']=c['fn']/c['truth'] if c['truth'] else None
        c['fp_rate']=c['fp']/c['predictions'] if c['predictions'] else None
    return {'report':{k:v for k,v in report.items() if k!='records'},'coverage':len(report['records']),
            'cameras':cameras,'totals':totals,'heatmap':heatmap,'cells':cells,'sizes':sizes,'groups':dict(groups),
            'failures':failures[offset:offset+12],'failure_count':len(failures),'offset':offset}
