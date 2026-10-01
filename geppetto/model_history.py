"""Permanently remove catalog models; retain only IDs that block re-import."""
import time
from fastapi import HTTPException
from .run_history import can_delete


def markers(store, db):
    return {m['id']:m for m in store.all(db,'model_history') if m.get('deleted_at')}


def ensure_visible(store, db, id):
    if id and id in markers(store,db):
        raise HTTPException(409,'This model has been deleted.')


def change(service, id, body, actor, action='delete'):
    if service.mode=='readonly':raise HTTPException(403,'Read-only mode.')
    if action!='delete':raise HTTPException(404,'Model restoration is not supported.')
    with service.store.connect() as db:
        model=service.store.get(db,'pinocchio',id)
        if body.get('name')!=model['name']:
            raise HTTPException(409,'The model name has changed. Check the target again.')
        runs=[r for r in service.store.all(db,'run') if r.get('pinocchio_id')==id]
        if any(not can_delete(r) for r in runs):
            raise HTTPException(409,'Some runs are active or their resource release is unconfirmed. Finish the runs before deleting.')
        run_ids={r['id'] for r in runs}
        schedules=[s for s in service.store.all(db,'schedule') if s.get('pinocchio_id')==id
                   or s.get('schedule',{}).get('run_id') in run_ids
                   or s.get('frozen',{}).get('parent_id') in run_ids]
        if any(s.get('enabled') for s in schedules):
            raise HTTPException(409,'Active schedules exist. Disable or cancel them before deleting.')
        now=time.time()
        # No restorable model or execution payload is kept in these tombstones.
        service.store.put(db,'model_history',{'id':id,'deleted_at':now})
        for run in runs:
            service.store.put(db,'deleted_run',{'id':run['id']})
        targets={('pinocchio',id)}|{('run',r) for r in run_ids}|{('run_history',r) for r in run_ids}
        targets|={('schedule',s['id']) for s in schedules}
        for kind in ('evaluation','run_history_event'):
            targets|={(kind,d['id']) for d in service.store.all(db,kind)
                      if d.get('run_id') in run_ids or d.get('model_id')==id}
        db.executemany('DELETE FROM documents WHERE kind=? AND id=?',targets)
        return {'id':id,'node_id':model['node_id'],'run_ids':sorted(run_ids),
                'changed':True,'action':'delete','files_deleted':False}
