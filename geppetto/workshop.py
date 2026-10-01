"""Geppetto's node / model / job hierarchy; metadata only, never provisions hosts."""
import re
import time
import uuid

LOCAL_WHALE='whale-local'


def new_pinocchio(store,db,node_id,template,name=''):
    number=max((p['number'] for p in store.all(db,'pinocchio')),default=0)+1
    item={'id':uuid.uuid4().hex,'number':number,'name':name.strip()[:160] or template.get('name') or 'New model',
          'model_id':None,'project_id':'project-'+template['category'],
          'node_id':node_id,'template_id':template['id'],'category':template['category'],'created_at':time.time()}
    item['model_id']=item['id']
    store.put(db,'pinocchio',item)
    return item


def reconcile(store):
    """Idempotently attach existing local records, preserving immutable job snapshots."""
    with store.connect() as db:
        nodes=store.all(db,'whale')
        if not any(n['id']==LOCAL_WHALE for n in nodes):
            store.put(db,'whale',{'id':LOCAL_WHALE,'name':'local','host':'local',
                'connection':'local-workspace','description':'Local Geppetto workspace · Not a remote node connection',
                'template_ids':[t['id'] for t in store.all(db,'template')],'created_at':time.time()})
        for d in store.all(db,'dataset'):
            if not d.get('node_id'):
                d['node_id']=LOCAL_WHALE;store.put(db,'dataset',d)
        # Parents and schedules precede dependent jobs. Existing jobs are distinct models unless explicitly related.
        documents=[('schedule',s) for s in store.all(db,'schedule')]+[('run',r) for r in sorted(store.all(db,'run'),key=lambda r:r['created_at'])]
        assigned={}
        for kind,job in documents:
            if job.get('pinocchio_id'):
                assigned[job['id']]=job['pinocchio_id'];continue
            node_id=job.get('node_id',LOCAL_WHALE)
            parent=job.get('schedule_id') or job.get('frozen',{}).get('parent_id')
            pid=assigned.get(parent)
            if not pid:
                template={'id':job.get('template_id'),'category':job['category']}
                pid=new_pinocchio(store,db,node_id,template)['id']
            job.update(node_id=node_id,pinocchio_id=pid)
            store.put(db,kind,job);assigned[job['id']]=pid

        # Each independent training creates a model. Only resume/evaluate retain identity.
        from .catalog import CATEGORIES
        for category in CATEGORIES:
            key='project-'+category['id']
            if not any(p['id']==key for p in store.all(db,'project')):
                store.put(db,'project',{'id':key,'name':category['name'],'category':category['id'],'created_at':time.time()})
        roots={}; assigned={}
        for run in sorted(store.all(db,'run'),key=lambda r:r['created_at']):
            relation=run.get('frozen',{}).get('relation','train')
            parent=run.get('frozen',{}).get('parent_id')
            pid=run['pinocchio_id']
            if relation in ('resume','evaluate') and parent in assigned:
                pid=assigned[parent]
            elif relation not in ('resume','evaluate'):
                if pid in roots:
                    old=store.get(db,'pinocchio',pid)
                    template={'id':run.get('template_id'),'category':run['category'],'name':old['name']}
                    new=new_pinocchio(store,db,run['node_id'],template)
                    new['project_id']=old.get('project_id','project-'+run['category'])
                    store.put(db,'pinocchio',new);pid=new['id']
                roots[pid]=run['id']
            run['pinocchio_id']=pid;run['model_id']=pid
            pin=store.get(db,'pinocchio',pid)
            run['project_id']=pin.get('project_id','project-'+run['category'])
            store.put(db,'run',run);assigned[run['id']]=pid
        for pin in store.all(db,'pinocchio'):
            if not pin.get('name_customized') and re.fullmatch(r'\d+번 피노키오',pin['name']):
                try:pin['name']=store.get(db,'template',pin['template_id'])['name']
                except KeyError:pin['name']='External model'
            pin['model_id']=pin['id'];pin.setdefault('project_id','project-'+pin['category'])
            store.put(db,'pinocchio',pin)
        for node in store.all(db,'whale'):
            if not node.get('name_customized') and (re.match(r'^\d+번 고래',node['name']) or node['name']=='가져온 기록'):
                node['name']=node.get('host') or 'local';store.put(db,'whale',node)


def topology(service):
    reconcile(service.store)
    with service.store.connect() as db:
        result={kind:service.store.all(db,kind) for kind in ('whale','pinocchio','project')}
        from .clusters import public
        clusters={c['id']:c for c in service.store.all(db,'cluster')}
        for w in result['whale']:
            if w.get('cluster_id') in clusters:
                c=clusters[w['cluster_id']]
                w.update(agent_state=public(c)['state'],cluster_name=c['name'],last_seen_at=c.get('last_seen_at'))
        from .service import TERMINAL
        hidden={m['id'] for m in service.store.all(db,'run_history') if m.get('deleted_at')}
        counts={}
        for run in service.store.all(db,'run'):
            if run['id'] in hidden and run['state'] in TERMINAL:
                pid=run.get('pinocchio_id')
                counts[pid]=counts.get(pid,0)+1
        for pin in result['pinocchio']:
            pin['hidden_run_count']=counts.get(pin['id'],0)
        from .model_history import markers as deleted_models
        archived=deleted_models(service.store,db)
        active_models={r.get('pinocchio_id') for r in service.store.all(db,'run') if r['state'] not in TERMINAL}
        result['pinocchio']=[p for p in result['pinocchio'] if p['id'] not in archived or p['id'] in active_models]
        result['evaluations']=[{k:r[k] for k in ('id','run_id','checkpoint','split','created_at','synthetic')} for r in service.store.all(db,'evaluation')]
        return result


def create_whale(service,body):
    if service.mode=='readonly':raise ValueError('Read-only mode')
    name=body.get('name','');host=body.get('host','')
    if not isinstance(name,str) or not name.strip() or len(name)>160 or not isinstance(host,str) or len(host)>300:
        raise ValueError('Provide a node name and optional host label')
    item={'id':uuid.uuid4().hex,'name':name.strip(),'host':host.strip(),'connection':'unconnected',
          'description':'Node information registered · Connection not configured yet','template_ids':[], 'created_at':time.time()}
    with service.store.connect() as db:service.store.put(db,'whale',item)
    return item


def create_pinocchio(service,node_id,body):
    if service.mode=='readonly':raise ValueError('Read-only mode')
    node=service.get('whale',node_id)
    template=service.get('template',body.get('template_id',''))
    if template['id'] not in node['template_ids']:raise ValueError('Template is not available inside this whale')
    name=body.get('name','')
    if not isinstance(name,str) or len(name)>160:raise ValueError('Invalid model name')
    with service.store.connect() as db:return new_pinocchio(service.store,db,node_id,template,name)


def rename(service,kind,id,body):
    if service.mode=='readonly':raise ValueError('Read-only mode')
    name=body.get('name')
    if not isinstance(name,str) or not name.strip() or len(name.strip())>160:
        raise ValueError('Name must contain 1–160 characters.')
    with service.store.connect() as db:
        item=service.store.get(db,kind,id)
        item.update(name=name.strip(),name_customized=True)
        service.store.put(db,kind,item)
    return item


def create_project(service,body):
    from .catalog import CATEGORIES
    if service.mode=='readonly':raise ValueError('Read-only mode')
    name=body.get('name');category=body.get('category')
    if not isinstance(name,str) or not name.strip() or len(name.strip())>160 or category not in [c['id'] for c in CATEGORIES]:
        raise ValueError('Check the project name and task category.')
    item={'id':uuid.uuid4().hex,'name':name.strip(),'category':category,'created_at':time.time()}
    with service.store.connect() as db:service.store.put(db,'project',item)
    return item


def move_project(service,id,body):
    if service.mode=='readonly':raise ValueError('Read-only mode')
    with service.store.connect() as db:
        pin=service.store.get(db,'pinocchio',id)
        project=service.store.get(db,'project',body.get('project_id',''))
        if pin['category']!=project['category']:raise ValueError('Select a project with the same task category.')
        pin['project_id']=project['id'];service.store.put(db,'pinocchio',pin)
        for run in service.store.all(db,'run'):
            if run.get('pinocchio_id')==id:
                run['project_id']=project['id'];service.store.put(db,'run',run)
    return pin
