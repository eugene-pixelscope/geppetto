"""Bounded typed training diagnostics; no executable HTML or arbitrary URLs."""
import math
import time
from .configuration import digest


def number(v):
    if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v):
        raise ValueError('Finite numeric values required')
    return v


def publish(service,run_id,body):
    if service.mode=='readonly':raise ValueError('Read-only mode')
    service.get('run',run_id)
    kind=body.get('type');name=body.get('name');step=body.get('step')
    if not isinstance(name,str) or not 1<=len(name)<=160 or type(step) is not int or step<0:
        raise ValueError('Name and nonnegative integer step required')
    data=body.get('data',{})
    if kind=='histogram':
        edges=data.get('edges',[]);counts=data.get('counts',[])
        if not 1<=len(counts)<=256 or len(edges)!=len(counts)+1:raise ValueError('Provide up to 256 bins and their edges')
        edges=list(map(number,edges));counts=list(map(number,counts))
        if any(a>=b for a,b in zip(edges,edges[1:])) or any(v<0 for v in counts):raise ValueError('Invalid histogram bins')
        data={'edges':edges,'counts':counts}
    elif kind=='embedding':
        points=data.get('points',[])
        if not 1<=len(points)<=5000:raise ValueError('Provide 1–5000 projected points')
        clean=[]
        for p in points:
            clean.append({'x':number(p['x']),'y':number(p['y']),'label':str(p.get('label',''))[:160]})
        projection=data.get('projection')
        if projection not in ('pca','tsne','umap','coordinates'):raise ValueError('Declare the projection method')
        data={'points':clean,'projection':projection}
    elif kind=='trace':
        events=data.get('events',[])
        if not 1<=len(events)<=10000:raise ValueError('Provide 1–10000 duration events')
        clean=[]
        for e in events:
            start=number(e['start_us']);duration=number(e['duration_us'])
            if start<0 or duration<0:raise ValueError('Negative trace time')
            clean.append({'name':str(e['name'])[:160],'lane':str(e.get('lane','CPU'))[:100],
                          'start_us':start,'duration_us':duration})
        if len({e['lane'] for e in clean})>128:raise ValueError('At most 128 trace lanes')
        data={'events':clean,'unit':'microseconds'}
    else:raise ValueError('Supported diagnostics: histogram, embedding, trace')
    item={'id':digest([run_id,kind,name,step])[:32],'run_id':run_id,'type':kind,'name':name,'step':step,'data':data,'created_at':time.time()}
    with service.store.connect() as db:
        old=next((v for v in service.store.all(db,'visualization') if v['id']==item['id']),None)
        if old:
            if old['data']!=data:raise ValueError('This diagnostic step is already published')
            return old
        if sum(v['run_id']==run_id for v in service.store.all(db,'visualization'))>=1000:raise ValueError('Run diagnostic quota reached')
        service.store.put(db,'visualization',item)
    return item
