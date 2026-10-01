"""Register the supplied real ball dataset; writes only the Geppetto catalog/index."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from geppetto.indexed_datasets import import_yolo_splits
from geppetto.storage import Store

parser=argparse.ArgumentParser()
parser.add_argument('--root',type=Path,required=True)
parser.add_argument('--splits-dir',type=Path,required=True)
parser.add_argument('--home',type=Path,default=Path(__file__).resolve().parents[1]/'local/app')
a=parser.parse_args()
item=import_yolo_splits(Store(a.home/'catalog.sqlite3'),a.home,a.root,
    {split:a.splits_dir/('soccer_ball_pose_'+file+'.txt') for split,file in [('train','train'),('validation','val'),('test','test')]},
    'Soccer Ball · Stage3 v4',{'format':'yolo-pose','unit':'image','classes':{'0':'ball'},'kpt_shape':[1,3],
        'split_strategy':'recording-timestamp','source_config':'soccer_ball_pose_split.yaml'})
print(json.dumps({k:item[k] for k in ('id','name','root','splits','sample_count','status','verification_note')},ensure_ascii=False,indent=2))
