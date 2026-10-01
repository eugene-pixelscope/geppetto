"""Bounded reproducible grid/random searches with persistent trial identities."""
import copy
import itertools
import json
import math
import random
import time
from .configuration import digest
from .service import TERMINAL


def candidates(body):
    space = body.get('parameters')
    if not isinstance(space, dict) or not 1 <= len(space) <= 10:
        raise ValueError('Provide 1–10 parameter paths with explicit choices')
    paths = sorted(space)
    for path, values in space.items():
        if not isinstance(path, str) or not path or any(not p or p.startswith('_') for p in path.split('.')):
            raise ValueError('Invalid parameter path')
        if not isinstance(values, list) or not 1 <= len(values) <= 100 or any(isinstance(v, (dict, list)) for v in values):
            raise ValueError('Each parameter requires 1–100 scalar choices')
        if len({json.dumps(v, allow_nan=False) for v in values}) != len(values):
            raise ValueError('Duplicate parameter choices')
    budget = body.get('max_trials', 10)
    seed = body.get('seed', 0)
    if type(budget) is not int or not 1 <= budget <= 100 or type(seed) is not int:
        raise ValueError('Use a 1–100 trial budget and integer seed')
    total = math.prod(len(space[p]) for p in paths)
    if total > 1000000000:
        raise ValueError('Search space exceeds one billion combinations')
    method = body.get('method', 'grid')
    if method == 'grid':
        indexes = range(min(total, budget))
    elif method in ('random','tpe','hyperband'):
        indexes = random.Random(seed).sample(range(total), min(total, budget))
    else:
        raise ValueError('Supported search methods are grid, random, tpe and hyperband')
    result = []
    for index in indexes:
        config = {}
        for p in reversed(paths):
            values = space[p]
            config[p] = values[index % len(values)]
            index //= len(values)
        result.append(config)
    return result


def preview(service, body):
    goal = body.get('metric')
    if not isinstance(goal, str) or not goal or len(goal) > 160 or body.get('direction') not in ('maximize', 'minimize'):
        raise ValueError('Specify an objective metric and direction')
    concurrency = body.get('concurrency', 1)
    if type(concurrency) is not int or not 1 <= concurrency <= 64:
        raise ValueError('Sweep concurrency must be 1–64')
    limits=body.get('limits',{})
    if not isinstance(limits,dict) or set(limits)-{'gpu_seconds','cost','hourly_rate'}:raise ValueError('Invalid budget fields')
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0 for v in limits.values()):raise ValueError('Budgets and rates must be finite positive numbers')
    if 'cost' in limits and 'hourly_rate' not in limits:raise ValueError('Cost budget requires an explicit GPU hourly rate')
    pruning=body.get('pruning')
    if pruning:
        if any(type(pruning.get(k)) is not int for k in ('min_step','max_step','reduction_factor')) or not 1<=pruning['min_step']<=pruning['max_step']<=100000 or not 2<=pruning['reduction_factor']<=10:
            raise ValueError('Provide valid successive-halving rungs')
    proposals=candidates(body)
    brackets=[]
    if body.get('method')=='hyperband':
        if 'epochs' in body['parameters']:raise ValueError('Hyperband manages epochs; search other parameters')
        if not pruning:raise ValueError('Hyperband requires min_step, max_step and reduction_factor')
        factor=pruning['reduction_factor']
        smax=int(math.floor(math.log(pruning['max_step']/pruning['min_step'],factor)))
        if len(proposals)<smax+1:raise ValueError('Provide at least one candidate for each Hyperband bracket')
        for s in range(smax,-1,-1):
            brackets.append({'id':s,'min_step':max(pruning['min_step'],int(pruning['max_step']/factor**s)),
                             'max_step':pruning['max_step'],'reduction_factor':factor,
                             'weight':(smax+1)*factor**s/(s+1)})
        quotas=[1]*len(brackets)
        for _ in range(len(proposals)-len(brackets)):
            selected=max(range(len(brackets)),key=lambda i:brackets[i]['weight']/(quotas[i]+1))
            quotas[selected]+=1
        assignments=[b['id'] for b,n in zip(brackets,quotas) for _ in range(n)]
    trials = []
    for index, parameters in enumerate(proposals):
        request = copy.deepcopy(body['training'])
        request['schedule'] = {'kind': 'now'}
        if request.get('relation', 'train') != 'train':
            raise ValueError('Sweeps create new training runs')
        overrides = request.setdefault('overrides', {})
        for path, value in parameters.items():
            target = overrides
            parts = path.split('.')
            for part in parts[:-1]:
                target = target.setdefault(part, {})
                if not isinstance(target, dict):
                    raise ValueError('Conflicting parameter paths')
            target[parts[-1]] = value
        if brackets:
            overrides['epochs']=pruning['max_step']+1
        request['name'] = body.get('name', 'Sweep')[:120] + ' · ' + str(index + 1)
        frozen = service.preview(request)
        if brackets and frozen['frozen']['template']['adapter']!='ball':raise ValueError('Hyperband epoch budgets currently require the ball adapter')
        trials.append({'bracket':assignments[index] if brackets else None,'request': {**request, 'confirmation_hash': frozen['confirmation_hash']},
                       'frozen': frozen['frozen'], 'parameters': parameters})
    spec = {'name': str(body.get('name', 'Sweep'))[:160], 'metric': goal, 'direction': body['direction'],
            'brackets':brackets,'limits':limits,'pruning':pruning,'concurrency': concurrency, 'method': body.get('method', 'grid'), 'seed': body.get('seed', 0), 'trials': trials}
    return {'spec': spec, 'confirmation_hash': digest(spec)}


def create(service, body):
    value = preview(service, body)
    if body.get('confirmation_hash') != value['confirmation_hash']:
        raise ValueError('Review the generated trials before creating the sweep')
    key = body.get('request_id', '')
    import re
    if not re.fullmatch('[a-f0-9]{32}', key):
        raise ValueError('request_id must be UUID hex')
    item = {'id': key, **value['spec'], 'digest': value['confirmation_hash'], 'created_at': time.time(), 'state': 'active'}
    with service.store.connect() as db:
        old = next((s for s in service.store.all(db, 'sweep') if s['id'] == key), None)
        if old:
            if old['digest'] != item['digest']:
                raise ValueError('Sweep request ID was reused with different settings')
            return old
        # Publish all frozen runs atomically. A crash cannot create only half a sweep.
        from .workshop import new_pinocchio
        for i, trial in enumerate(item['trials']):
            frozen = trial['frozen']
            pin = new_pinocchio(service.store, db, frozen['node_id'], frozen['template'], trial['request']['name'])
            pin['project_id'] = frozen['project_id']
            service.store.put(db, 'pinocchio', pin)
            run = service.new_run({'id': digest([key, i])[:32], 'name': trial['request']['name'],
                'created_at': time.time(), 'template_id': frozen['template']['id'], 'category': frozen['template']['category'],
                'frozen': frozen, 'synthetic': service.mode == 'demo', 'node_id': frozen['node_id'],
                'pinocchio_id': pin['id'], 'model_id': pin['id'], 'project_id': pin['project_id'],
                'sweep_id': key, 'sweep_bracket':trial['bracket'], 'priority': frozen.get('priority', 0)})
            trial['run_id'] = run['id']
            service.store.put(db, 'run', run)
        service.store.put(db, 'sweep', item)
    return item


def status(service, id):
    item = service.get('sweep', id)
    runs = [service.get('run', t['run_id']) for t in item['trials']]
    scores = [{'run_id': r['id'], 'name': r['name'], 'state': r['state'],
               'value': r['metrics'].get(item['metric'], [{}])[-1].get('value') if r['metrics'].get(item['metric']) else None}
              for r in runs]
    ranked = sorted((r for r in scores if r['state'] == 'succeeded' and r['value'] is not None),
                    key=lambda r: r['value'], reverse=item['direction'] == 'maximize')
    return {**item, 'state': 'completed' if item['state'] == 'active' and all(r['state'] in TERMINAL for r in runs) else item['state'],
            'scores': scores, 'best': ranked[0] if ranked else None}


def cancel(service, id):
    if service.mode == 'readonly':
        raise ValueError('Read-only mode')
    with service.store.connect() as db:
        item = service.store.get(db, 'sweep', id)
        item.update(state='cancelled', cancelled_at=time.time())
        for trial in item['trials']:
            run = service.store.get(db, 'run', trial['run_id'])
            if run['state'] not in TERMINAL:
                run['stop_requested_at'] = time.time()
                service.store.put(db, 'run', run)
        service.store.put(db, 'sweep', item)
    return item


def can_dispatch(store, db, run, runs):
    if not run.get('sweep_id'):
        return True
    sweep = store.get(db, 'sweep', run['sweep_id'])
    if sweep['state'] != 'active':
        return False
    active = sum(bool(r.get('sweep_id') == sweep['id'] and r['state'] not in TERMINAL and
                 (r.get('dispatch') or r.get('airflow') or r['state'] == 'running')) for r in runs)
    return active < sweep['concurrency'] and (sweep.get('method')!='tpe' or tpe_next(sweep,runs)==run['id'])


def control(store, db, runs, now=None):
    """Persistent asynchronous halving and allocated GPU-time/cost ceilings."""
    now=time.time() if now is None else now
    for sweep in store.all(db,'sweep'):
        if sweep['state']!='active':continue
        members=[r for r in runs if r.get('sweep_id')==sweep['id']]
        seconds=sum(max(0,r.get('finished_at',now)-r['started_at'])*r['frozen']['gpu']['count']
                    for r in members if r.get('started_at') is not None)
        limits=sweep.get('limits',{})
        exhausted=(limits.get('gpu_seconds') is not None and seconds>=limits['gpu_seconds'] or
                   limits.get('cost') is not None and seconds/3600*limits['hourly_rate']>=limits['cost'])
        sweep['allocated_gpu_seconds']=seconds
        sweep['estimated_cost']=seconds/3600*limits.get('hourly_rate',0)
        if exhausted:
            sweep.update(state='budget_exhausted',budget_exhausted_at=now)
            for run in members:
                if run['state'] not in TERMINAL:
                    run.update(stop_requested_at=now,stop_reason='sweep budget exhausted')
                    store.put(db,'run',run)
        pruning=sweep.get('pruning')
        if pruning and not exhausted:
            brackets=sweep.get('brackets') or [dict(pruning,id=None)]
            for bracket in brackets:
                cohort=[r for r in members if r.get('sweep_bracket')==bracket['id']]
                factor=bracket['reduction_factor'];rung=bracket['min_step']
                while rung<bracket['max_step']:
                    reached=[]
                    for run in cohort:
                        points=run.get('metrics',{}).get(sweep['metric'],[])
                        point=next((p for p in points if p.get('step',-1)>=rung),None)
                        if point:reached.append((run,point['value']))
                    if len(reached)>=factor:
                        ordered=sorted(reached,key=lambda pair:pair[1],reverse=sweep['direction']=='maximize')
                        keep=max(1,math.ceil(len(ordered)/factor))
                        for run,value in ordered[keep:]:
                            if run['state'] not in TERMINAL and not run.get('stop_requested_at'):
                                run.update(stop_requested_at=now,stop_reason='successive halving',pruned_at_step=rung)
                                store.put(db,'run',run)
                    rung*=factor
        store.put(db,'sweep',sweep)


def tpe_next(sweep,runs):
    """Discrete Tree Parzen likelihood ratio over reviewed frozen candidates."""
    trials={t['run_id']:t for t in sweep['trials']}
    members=[r for r in runs if r.get('sweep_id')==sweep['id']]
    pending=[r for r in members if r['state']=='queued' and not r.get('dispatch') and not r.get('stop_requested_at')]
    if not pending:return None
    scores=[(r,r['metrics'][sweep['metric']][-1]['value']) for r in members
            if r['state']=='succeeded' and r.get('metrics',{}).get(sweep['metric'])]
    if len(scores)<4:return min(pending,key=lambda r:r['created_at'])['id']
    scores.sort(key=lambda pair:pair[1],reverse=sweep['direction']=='maximize')
    split=max(1,len(scores)//4);good=scores[:split];bad=scores[split:]
    choices={p:{json.dumps(t['parameters'][p],sort_keys=True) for t in trials.values()} for p in next(iter(trials.values()))['parameters']}
    def likelihood(run):
        score=0
        for p,v in trials[run['id']]['parameters'].items():
            # Laplace-smoothed categorical Parzen densities are defined for all choices.
            g=sum(trials[r['id']]['parameters'][p]==v for r,_ in good)
            b=sum(trials[r['id']]['parameters'][p]==v for r,_ in bad)
            score+=math.log((g+1)/(len(good)+len(choices[p])))-math.log((b+1)/(len(bad)+len(choices[p])))
        return score
    return max(pending,key=lambda r:(likelihood(r),-r['created_at']))['id']
