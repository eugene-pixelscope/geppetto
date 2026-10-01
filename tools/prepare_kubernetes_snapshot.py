"""Copy a live local catalog for first deployment; never overwrite a deployed PVC."""
import argparse
import json
from pathlib import Path
import shutil
import sqlite3

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--source',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args()
a.output.mkdir(parents=True,exist_ok=False)
with sqlite3.connect(a.source/'catalog.sqlite3') as source,sqlite3.connect(a.output/'catalog.sqlite3') as target:
    source.backup(target)
    for id,body in target.execute("SELECT id,body FROM documents WHERE kind='dataset'").fetchall():
        d=json.loads(body)
        if d.get('sample_index'):
            relative=Path(d['sample_index']).relative_to(a.source.resolve())
            d['sample_index']='/state/'+relative.as_posix()
        for s in d.get('source_splits',{}).values():
            if s.get('snapshot'):
                s['snapshot']='/state/'+Path(s['snapshot']).relative_to(a.source.resolve()).as_posix()
        target.execute("UPDATE documents SET body=? WHERE kind='dataset' AND id=?",(json.dumps(d,ensure_ascii=False),id))
if (a.source/'dataset-indexes').exists():shutil.copytree(a.source/'dataset-indexes',a.output/'dataset-indexes')
print('Catalog snapshot prepared; source unchanged')
