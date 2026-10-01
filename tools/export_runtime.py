"""Export reviewed local records into container/DAG registries. Never deploys."""
import argparse
import copy
import json
from pathlib import Path
import re
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from geppetto.configuration import digest
from geppetto.storage import Store

parser=argparse.ArgumentParser()
parser.add_argument('--database',type=Path,required=True)
parser.add_argument('--template',required=True)
parser.add_argument('--dataset',required=True)
parser.add_argument('--dataset-pvc',required=True)
parser.add_argument('--dataset-subpath',default='')
parser.add_argument('--output',type=Path,required=True)
a=parser.parse_args()
store=Store(a.database)
with store.connect() as db:
    template=store.get(db,'template',a.template)
    dataset=store.get(db,'dataset',a.dataset)
if not template.get('live_verified') or not template.get('validation_evidence'):
    parser.error('Only independently validated templates can be exported for execution')
if not re.fullmatch(r'.+@sha256:[a-f0-9]{64}',template.get('image_digest','')):
    parser.error('Immutable image digest required')
if dataset.get('synthetic') or dataset['category']!=template['category']:
    parser.error('A compatible real dataset is required')
if Path(a.dataset_subpath).is_absolute() or '..' in Path(a.dataset_subpath).parts:
    parser.error('Dataset subpath must stay inside its PVC')
a.output.mkdir(parents=True,exist_ok=True)
for name in ('datasets.json','template-allowlist.json'):
    if (a.output/name).exists(): parser.error('Refusing to overwrite an existing registry')
mounted=copy.deepcopy(dataset)
mounted['root']='/datasets/input'
(a.output/'datasets.json').write_text(json.dumps({dataset['id']:mounted},indent=2,ensure_ascii=False))
allowlist={template['id']:{'image':template['image_digest'],'template_sha256':digest(template),
    'datasets':{dataset['id']:{'version':dataset['version'],'pvc':a.dataset_pvc,'subpath':a.dataset_subpath}}}}
(a.output/'template-allowlist.json').write_text(json.dumps(allowlist,indent=2))
print(a.output)
