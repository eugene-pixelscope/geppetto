"""Export real YOLO predictions for Geppetto; never starts training.

Input manifest: dataset_id, dataset_version, root, split and samples from Geppetto's
sample API (global index + relative image path). Sampling is done before inference.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


def export(model, manifest, weights, output, imgsz=1920, device='cpu', floor=.01):
    root=Path(manifest['root']).resolve(strict=True)
    samples=manifest['samples']
    if manifest['split'] not in ('validation','test') or not 1<=len(samples)<=100000:
        raise ValueError('Expected validation/test sample manifest')
    if not 0<=floor<=1:raise ValueError('Invalid prediction floor')
    with Path(weights).open('rb') as stream:sha=hashlib.file_digest(stream,'sha256').hexdigest()
    images=[];seen=set()
    for i,sample in enumerate(samples):
        index=sample['index'];path=(root/sample['image']).resolve(strict=True)
        if not path.is_relative_to(root) or type(index) is not int or index<0 or index in seen:
            raise ValueError('Invalid or duplicate sample')
        if sample.get('split',manifest['split'])!=manifest['split']:raise ValueError('Mixed splits')
        seen.add(index)
        # Original-image normalized boxes, including images with no detections.
        result=model.predict(source=str(path),imgsz=imgsz,device=device,conf=floor,
                             max_det=300,verbose=False,save=False)[0]
        predictions=[]
        for coords,score,cls in zip(result.boxes.xywhn.cpu().tolist(),result.boxes.conf.cpu().tolist(),result.boxes.cls.cpu().tolist()):
            if int(cls)==0:predictions.append({'box':coords,'score':score,'class_id':0})
        images.append({'index':index,'predictions':predictions})
        if (i+1)%16==0 or i+1==len(samples):print(f'Predicted {i+1}/{len(samples)}',flush=True)
    with Path(weights).open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=sha:raise ValueError('Checkpoint changed during inference')
    body={'dataset_id':manifest['dataset_id'],'dataset_version':manifest['dataset_version'],
          'split':manifest['split'],'checkpoint':'sha256:'+sha,'prediction_floor':floor,'images':images}
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    tmp=output.with_suffix('.tmp');tmp.write_text(json.dumps(body,allow_nan=False));tmp.replace(output)
    return body


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',required=True,type=Path);p.add_argument('--weights',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path);p.add_argument('--imgsz',type=int,default=1920)
    p.add_argument('--device',default='cpu');p.add_argument('--floor',type=float,default=.01)
    p.add_argument('--ultralytics-root',type=Path)
    args=p.parse_args()
    if args.ultralytics_root:sys.path.insert(0,str(args.ultralytics_root))
    os.environ.setdefault('YOLO_CONFIG_DIR','/tmp/geppetto-yolo')
    from ultralytics import YOLO
    model=YOLO(str(args.weights))
    export(model,json.loads(args.manifest.read_text()),args.weights,args.output,args.imgsz,args.device,args.floor)


if __name__=='__main__':main()
