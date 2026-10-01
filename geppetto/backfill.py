"""Preview and atomically create missing cron occurrences in a bounded historical interval."""
import copy
from datetime import datetime
import time
from .configuration import digest
from .service import next_time


def timestamp(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError('Use timestamps with explicit UTC offsets')
    return dt.timestamp()


def spec(schedule, body, runs):
    if schedule['schedule']['kind'] != 'cron':
        raise ValueError('Only recurring schedules support backfill')
    start, end = timestamp(body['start']), timestamp(body['end'])
    if start > end or end > time.time() or end - start > 366 * 86400:
        raise ValueError('Select a past interval of at most 366 days')
    existing = {r['id'] for r in runs}
    existing.update(h.get('run_id') for h in schedule.get('history', []) if h.get('run_id'))
    dates = []
    at = next_time(schedule['schedule']['cron'], schedule['timezone'], start - .001)
    while at <= end:
        key = digest({'schedule': schedule['id'], 'at': at})[:32]
        dates.append({'at': at, 'run_id': key, 'exists': key in existing})
        if len(dates) > 500:
            raise ValueError('Select at most 500 occurrences per backfill')
        at = next_time(schedule['schedule']['cron'], schedule['timezone'], at)
    return {'schedule_id': schedule['id'], 'start': start, 'end': end, 'timezone': schedule['timezone'],
            'frozen': schedule['frozen'], 'occurrences': dates}


def preview(service, id, body):
    with service.store.connect() as db:
        value = spec(service.store.get(db, 'schedule', id), body, service.store.all(db, 'run'))
    return {'spec': value, 'confirmation_hash': digest(value)}


def create(service, id, body):
    if service.mode == 'readonly':
        raise ValueError('Read-only mode')
    with service.store.connect() as db:
        schedule = service.store.get(db, 'schedule', id)
        value = spec(schedule, body, service.store.all(db, 'run'))
        key = body.get('confirmation_hash')
        previous = next((b for b in service.store.all(db, 'backfill') if b['id'] == key), None)
        if previous:
            return previous
        if digest(value) != key:
            raise ValueError('Review the current missing occurrences before backfilling')
        from .model_history import ensure_visible
        ensure_visible(service.store, db, schedule.get('pinocchio_id'))
        result = {'id': key, 'schedule_id': id, 'created_at': time.time(), 'run_ids': []}
        for occurrence in value['occurrences']:
            if occurrence['exists']:
                continue
            run = occurrence_run(service, db, schedule, occurrence['at'], time.time())
            run['backfill_id'] = key
            service.store.put(db, 'run', run)
            result['run_ids'].append(run['id'])
        service.store.put(db, 'backfill', result)
        return result


def occurrence_run(service, db, schedule, at, now):
    from .workshop import new_pinocchio
    frozen = copy.deepcopy(schedule['frozen'])
    pin = new_pinocchio(service.store, db, frozen['node_id'], frozen['template'])
    pin['project_id'] = frozen['project_id']
    service.store.put(db, 'pinocchio', pin)
    run = service.new_run({k: copy.deepcopy(schedule[k]) for k in ('name', 'category', 'template_id', 'synthetic')})
    run.update(id=digest({'schedule': schedule['id'], 'at': at})[:32], frozen=frozen,
               created_at=now, logical_date=at, schedule_id=schedule['id'], node_id=frozen['node_id'],
               pinocchio_id=pin['id'], model_id=pin['id'], project_id=frozen['project_id'], priority=frozen.get('priority', 0))
    return run
