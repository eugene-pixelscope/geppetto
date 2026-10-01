"""Reversible history deletion; independent of imported run documents and schedulers."""
import time
import uuid
from fastapi import HTTPException
from .service import TERMINAL


def can_delete(run):
    if run.get('state') not in TERMINAL:
        return False
    managed = run.get('frozen', {}).get('execution', {}).get('backend') == 'agent'
    return not (managed and run.get('dispatch', {}).get('job_uid') and not run.get('resource_released'))


def decorate(run, marker=None):
    # A source that reopens an imported run must never hide active work.
    deleted = bool(marker and marker.get('deleted_at') and run.get('state') in TERMINAL)
    return {**run, 'history_can_delete': can_delete(run),
            'history_deletion': marker if deleted else None}


def page(service, category=None, offset=0, limit=50, visibility='active'):
    if visibility not in ('active', 'deleted', 'all'):
        raise HTTPException(422, 'Check the history view filters.')
    with service.store.connect() as db:
        markers = {m['id']: m for m in service.store.all(db, 'run_history')}
        from .model_history import markers as model_markers
        models=model_markers(service.store,db)
        items = [decorate(r, effective_marker(r,markers.get(r['id']),models)) for r in service.store.all(db, 'run')]
    items = [r for r in items if (not category or r.get('category') == category) and
             (visibility == 'all' or bool(r['history_deletion']) == (visibility == 'deleted'))]
    if visibility == 'deleted':
        items.sort(key=lambda r: r['history_deletion']['deleted_at'], reverse=True)
    return {'items': items[offset:offset+limit], 'total': len(items), 'offset': offset, 'limit': limit}


def detail(service, run):
    with service.store.connect() as db:
        try:
            marker = service.store.get(db, 'run_history', run['id'])
        except KeyError:
            marker = None
        from .model_history import markers as model_markers
        marker=effective_marker(run,marker,model_markers(service.store,db))
    return decorate(run, marker)


def change(service, body, actor, action):
    if service.mode == 'readonly':
        raise HTTPException(403, 'History cannot be changed in read-only mode.')
    ids = body.get('ids')
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 100 or
            any(not isinstance(id, str) or not id or len(id)>200 for id in ids) or len(set(ids)) != len(ids)):
        raise HTTPException(422, 'Select 1–100 distinct run IDs.')
    if body.get('confirmation') != action:
        raise HTTPException(422, 'Confirm the history change.')
    with service.store.connect() as db:
        runs = [service.store.get(db, 'run', id) for id in ids]
        if action == 'restore':
            from .model_history import ensure_visible
            for run in runs:ensure_visible(service.store,db,run.get('pinocchio_id'))
        if action == 'delete':
            blocked = [r['id'] for r in runs if not can_delete(r)]
            if blocked:
                raise HTTPException(409, 'Cannot delete active runs or runs with unconfirmed resource release: '+', '.join(blocked))
        now = time.time()
        changed = []
        for run in runs:
            try:
                marker = service.store.get(db, 'run_history', run['id'])
            except KeyError:
                marker = {'id': run['id']}
            if bool(marker.get('deleted_at')) == (action == 'delete'):
                continue
            marker.update(deleted_at=now if action == 'delete' else None,
                          updated_at=now, actor_id=actor['id'], actor_name=actor['username'])
            service.store.put(db, 'run_history', marker)
            service.store.put(db, 'run_history_event', {'id':uuid.uuid4().hex, 'run_id':run['id'],
                              'action':action, 'at':now, 'actor_id':actor['id'], 'actor_name':actor['username']})
            changed.append(run['id'])
    return {'changed': changed, 'action': action, 'files_deleted': False}


def effective_marker(run, marker, models):
    model=models.get(run.get('pinocchio_id'))
    return {**model,'id':run['id'],'model_id':model['id']} if model else marker
