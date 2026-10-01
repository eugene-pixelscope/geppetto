"""Read-only deployment and managed-run verification, with a shareable report."""
import argparse
import hashlib
import json
from pathlib import Path
import ssl
import time
import httpx


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url',required=True);p.add_argument('--ca',type=Path,required=True);p.add_argument('--token-file',type=Path,required=True)
    p.add_argument('--expect',action='append',default=[],metavar='RUN_ID=STATE');p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    with httpx.Client(base_url=args.url.rstrip('/')+'/api/',verify=ssl.create_default_context(cafile=str(args.ca)),
                      headers={'Authorization':'Bearer '+args.token_file.read_text().strip()},timeout=60) as client:
        def get(path):
            r=client.get(path);r.raise_for_status();return r.json()
        report={'observed_at':time.time(),'health':get('health'),'runs':[]}
        for expectation in args.expect:
            id,state=expectation.split('=',1);run=get('catalog/run/'+id)
            if run['state']!=state:raise ValueError(id+' is '+run['state']+', expected '+state)
            if not run.get('resource_released'):raise ValueError(id+' resources have not been confirmed released')
            value={k:run.get(k) for k in ('id','name','state','resource_released','model_version','started_at','finished_at')}
            value.update(image=run['frozen']['execution']['profile']['image'],artifact_count=len(run['artifacts']),metrics=list(run['metrics']))
            for name in ('allocation.json','resume-verification.json'):
                artifact=next((a for a in run['artifacts'] if a['path']==name),None)
                if artifact:
                    response=client.get('runs/'+id+'/artifacts/'+artifact['id']);response.raise_for_status()
                    if hashlib.sha256(response.content).hexdigest()!=artifact['sha256']:raise ValueError('Artifact content differs')
                    value[name]=response.json()
            verification=value.get('resume-verification.json',{})
            if verification.get('restored_state_matches') and not all(verification['restored_state_matches'].values()):raise ValueError('Resume state mismatch')
            report['runs'].append(value)
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print('Verified',len(report['runs']),'runs;',args.output)

if __name__=='__main__':main()
