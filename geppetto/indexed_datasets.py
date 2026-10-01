"""Read-only catalog indexing of existing splits without scanning image payloads."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import sqlite3
import time

from .configuration import digest
from .files import fingerprint


def import_yolo_splits(store, home, root, splits, name, policy=None):
    root=Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Dataset root must be a directory')
    snapshots={}
    for split,file in splits.items():
        if split not in ('train','validation','test'):
            raise ValueError('Unsupported split')
        path=Path(file).resolve(strict=True)
        raw=path.read_bytes()
        snapshots[split]={'path':str(path),'sha256':hashlib.sha256(raw).hexdigest(),'raw':raw}
    policy=policy or {}
    version=digest({'root':str(root),'splits':{k:v['sha256'] for k,v in snapshots.items()},'policy':policy})
    id='ball-stage3-'+version[:16]
    with store.connect() as db:
        existing=next((d for d in store.all(db,'dataset') if d['id']==id),None)
    if existing:
        return existing
    output=Path(home)/'dataset-indexes'/id
    output.mkdir(parents=True,exist_ok=True)
    index=output/'samples.sqlite3'
    counts={s:0 for s in ('train','validation','test')}
    previews=[]
    with sqlite3.connect(index) as db:
        db.execute('CREATE TABLE IF NOT EXISTS samples (idx INTEGER PRIMARY KEY, image TEXT UNIQUE NOT NULL, label TEXT NOT NULL, split TEXT NOT NULL)')
        db.execute('CREATE INDEX IF NOT EXISTS samples_split ON samples(split,idx)')
        db.execute('DELETE FROM samples')
        idx=0
        for split,source in snapshots.items():
            (output/(split+'.txt')).write_bytes(source['raw'])
            for line in source['raw'].decode().splitlines():
                if not line.strip():
                    continue
                path=Path(line.strip())
                relative=path.relative_to(root).as_posix() if path.is_absolute() else path.as_posix()
                parts=PurePosixPath(relative).parts
                if '..' in parts or not parts or parts[0]!='images':
                    raise ValueError('Split image is outside dataset images root')
                label=str(PurePosixPath('labels',*parts[1:]).with_suffix('.txt'))
                try:
                    db.execute('INSERT INTO samples VALUES(?,?,?,?)',(idx,relative,label,split))
                except sqlite3.IntegrityError as exc:
                    raise ValueError('Duplicate image across split lists: '+relative) from exc
                if counts[split]<4:
                    previews.append({'index':idx,'image':relative,'label':label,'split':split,
                                     'image_file':fingerprint(root,relative),'label_file':fingerprint(root,label)})
                counts[split]+=1
                idx+=1
    if not idx:
        raise ValueError('Split lists are empty')
    item={'id':id,'name':name,'category':'ball-detection','root':str(root),'version':version,
          'version_basis':'split-index','status':'indexed','content_verified':False,
          'sample_count':idx,'samples':previews,'sample_index':str(index),'splits':counts,
          'policy':policy,'created_at':time.time(),'synthetic':False,'effective_samples':None,
          'source_splits':{k:{'path':v['path'],'sha256':v['sha256'],'snapshot':str(output/(k+'.txt'))} for k,v in snapshots.items()},
          'verification_note':'Split lists and preview samples were checked. Full image and label content validation has not been performed yet.'}
    with store.connect() as db:
        store.put(db,'dataset',item)
    return item


def sample_page(dataset, split=None, offset=0, limit=12):
    if dataset.get('sample_index'):
        with sqlite3.connect('file:'+dataset['sample_index']+'?mode=ro',uri=True) as db:
            where=' WHERE split=?' if split else ''
            params=(split,) if split else ()
            total=db.execute('SELECT COUNT(*) FROM samples'+where,params).fetchone()[0]
            if dataset.get('content_verified'):
                rows=db.execute('SELECT record FROM samples'+where+' ORDER BY idx LIMIT ? OFFSET ?',(*params,limit,offset)).fetchall()
                items=[json.loads(r[0]) for r in rows]
            else:
                rows=db.execute('SELECT idx,image,label,split FROM samples'+where+' ORDER BY idx LIMIT ? OFFSET ?',(*params,limit,offset)).fetchall()
                items=[dict(zip(('index','image','label','split'),r)) for r in rows]
    else:
        rows=[{**s,'index':i} for i,s in enumerate(dataset['samples']) if not split or s['split']==split]
        total=len(rows)
        items=rows[offset:offset+limit]
    return {'items':items,'total':total,'offset':offset,'limit':limit}


def sample_at(dataset,index):
    if index<0:
        raise KeyError('Sample index must be nonnegative')
    if dataset.get('sample_index'):
        with sqlite3.connect('file:'+dataset['sample_index']+'?mode=ro',uri=True) as db:
            row=db.execute('SELECT idx,image,label,split FROM samples WHERE idx=?',(index,)).fetchone()
        if not row:
            raise KeyError('Sample not found')
        sample=dict(zip(('index','image','label','split'),row))
        if dataset.get('content_verified'):
            with sqlite3.connect('file:'+dataset['sample_index']+'?mode=ro',uri=True) as db:
                record=db.execute('SELECT record FROM samples WHERE idx=?',(index,)).fetchone()
            if record:return json.loads(record[0])
        preview=next((s for s in dataset['samples'] if s['index']==index),None)
        return preview or sample
    if index>=len(dataset['samples']):
        raise KeyError('Sample not found')
    return dataset['samples'][index]
