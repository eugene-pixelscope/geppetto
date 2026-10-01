"""Remove explicitly synthetic catalog records; never unlink dataset or artifact files."""
import time


def clean_synthetic(store):
    with store.connect() as db:
        rows={k:store.all(db,k) for k in ('run','schedule','evaluation','dataset','pinocchio')}
        removed={k:set() for k in rows}
        # Real imported history is protected even if a source has an inconsistent synthetic flag.
        for kind in ('run','schedule','evaluation'):
            removed[kind]={r['id'] for r in rows[kind] if r.get('synthetic') is True and not r.get('imported')}
        # Keep dependencies that are still referenced by a real record.
        kept_jobs=[r for k in ('run','schedule') for r in rows[k] if r['id'] not in removed[k]]
        kept_evals=[r for r in rows['evaluation'] if r['id'] not in removed['evaluation']]
        for e in kept_evals:removed['run'].discard(e.get('run_id'))
        kept_jobs=[r for k in ('run','schedule') for r in rows[k] if r['id'] not in removed[k]]
        for r in kept_jobs:
            removed['run'].discard(r.get('frozen',{}).get('parent_id'))
            removed['schedule'].discard(r.get('schedule_id'))
        # Iteratively protect dependency ancestors.
        changed=True
        while changed:
            before=sum(map(len,removed.values()))
            for k in ('run','schedule'):
                for r in rows[k]:
                    if r['id'] in removed[k]:continue
                    removed['run'].discard(r.get('frozen',{}).get('parent_id'))
                    removed['run'].discard(r.get('schedule',{}).get('run_id'))
                    removed['schedule'].discard(r.get('schedule_id'))
            changed=before!=sum(map(len,removed.values()))
        remaining=[r for k in ('run','schedule') for r in rows[k] if r['id'] not in removed[k]]
        referenced_datasets={d['id'] for r in remaining for d in r.get('frozen',{}).get('datasets',[])}|{e.get('dataset_id') for e in kept_evals}
        removed['dataset']={d['id'] for d in rows['dataset'] if d.get('synthetic') is True and d['id'] not in referenced_datasets}
        used_pins={r.get('pinocchio_id') for r in remaining}
        test_pins={r.get('pinocchio_id') for k in ('run','schedule') for r in rows[k] if r['id'] in removed[k]}
        removed['pinocchio']={p['id'] for p in rows['pinocchio'] if p['id'] in test_pins and p['id'] not in used_pins}
        for kind,ids in removed.items():
            for id in ids:
                db.execute('DELETE FROM documents WHERE kind=? AND id=?',(kind,id))
                db.execute('DELETE FROM requests WHERE kind=? AND id=?',(kind,id))
        store.put(db,'workspace',{'id':'settings','demo_disabled':True,'cleaned_at':time.time()})
    return {k:len(v) for k,v in removed.items()}
