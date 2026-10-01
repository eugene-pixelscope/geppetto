"""Build a managed model image with every file from its reviewed source manifest."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

p=argparse.ArgumentParser();p.add_argument('--template',required=True);p.add_argument('--source',type=Path,required=True);p.add_argument('--base',required=True);p.add_argument('--tag',required=True);p.add_argument('--profile',default='mlops');a=p.parse_args()
root=Path(__file__).resolve().parents[1]
template=json.loads((root/'contracts/templates'/a.template).read_text())
with tempfile.TemporaryDirectory(prefix='geppetto-build-') as folder:
    context=Path(folder)
    for name in ('geppetto','integrations','contracts'):shutil.copytree(root/name,context/name,ignore=shutil.ignore_patterns('__pycache__'))
    for name,sha in template['code_files'].items():
        source=(a.source/name).resolve(strict=True)
        if not source.is_relative_to(a.source.resolve()) or hashlib.sha256(source.read_bytes()).hexdigest()!=sha:raise ValueError('Source drift: '+name)
        target=context/'model-source'/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
    docker=(root/'deploy/model.Dockerfile').read_text().replace('COPY geppetto','COPY model-source/ /app/ball/\nCOPY geppetto')
    (context/'Dockerfile').write_text(docker)
    subprocess.run(['minikube','-p',a.profile,'image','build','-t',a.tag,'--build-opt','build-arg=MODEL_BASE='+a.base,str(context)],check=True)
