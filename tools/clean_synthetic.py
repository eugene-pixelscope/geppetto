"""Explicit cleanup with an online SQLite backup; defaults to a read-only inventory."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from geppetto.cleanup import clean_synthetic
from geppetto.storage import Store
parser=argparse.ArgumentParser()
parser.add_argument('--home',type=Path,default=Path(__file__).resolve().parents[1]/'local/app')
parser.add_argument('--apply',action='store_true')
a=parser.parse_args()
store=Store(a.home/'catalog.sqlite3')
if not a.apply:
    with store.connect() as db:
        print(json.dumps({k:[{'id':r['id'],'name':r.get('name'),'synthetic':r.get('synthetic'),'imported':r.get('imported')} for r in store.all(db,k)] for k in ('run','schedule','evaluation','dataset','pinocchio')},ensure_ascii=False,indent=2))
else:
    backup=a.home/('before-cleanup-'+str(time.time_ns())+'.sqlite3')
    with sqlite3.connect(store.path) as source,sqlite3.connect(backup) as target:source.backup(target)
    print(json.dumps({'backup':str(backup),'removed':clean_synthetic(store)},ensure_ascii=False,indent=2))
