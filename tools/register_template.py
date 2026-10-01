"""Register a new, reviewed template version into the local catalog.

Does not build images, submit training or connect to infrastructure.
"""
import argparse
import json
from pathlib import Path
import re
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jsonschema import Draft202012Validator
from geppetto.execution import template_entrypoint
from geppetto.catalog import templates
from geppetto.storage import Store

parser=argparse.ArgumentParser()
parser.add_argument('--database',type=Path,required=True)
parser.add_argument('--file',type=Path,required=True)
a=parser.parse_args()
item=json.loads(a.file.read_text())
if item['id'] in {t['id'] for t in templates()}:
    parser.error('Use a new template ID; builtin versions are immutable')
Draft202012Validator.check_schema(item['schema'])
Draft202012Validator(item['schema']).validate(item['defaults'])
if 'entrypoint' in item:
    try:
        template_entrypoint(item)
    except ValueError as error:
        parser.error(str(error))
elif item.get('live_verified'):
    parser.error('Live templates require an explicit entrypoint')
if item.get('live_verified'):
    if not re.fullmatch(r'.+@sha256:[a-f0-9]{64}',item.get('image_digest','')):
        parser.error('Live templates require an immutable image digest')
    if not item.get('code_files') or not item.get('validation_evidence'):
        parser.error('Live templates require code hashes and isolated validation evidence')
store=Store(a.database)
with store.connect() as db:
    if any(t['id']==item['id'] for t in store.all(db,'template')):
        parser.error('Existing versions cannot be replaced; create a new ID')
    store.put(db,'template',item)
print(item['id'])
