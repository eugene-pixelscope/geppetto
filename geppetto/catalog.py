import copy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT/'contracts').is_dir():
    ROOT=Path(sys.prefix)/'share/geppetto'
CATEGORIES = [
    {'id': 'ball-detection', 'name': 'Ball detection', 'icon': '◉'},
    {'id': 'jersey-detection', 'name': 'Jersey detection', 'icon': '▣'},
    {'id': 'jersey-recognition', 'name': 'Jersey recognition', 'icon': '№'},
]


def templates():
    """Adding templates requires only a reviewed JSON document, not core changes."""
    result = []
    for file in sorted((ROOT / 'contracts/templates').glob('*.json')):
        item = json.loads(file.read_text())
        if item.get('bundle'):
            bundle = json.loads((ROOT / 'contracts' / item['bundle']).read_text())
            item['schema'] = bundle['schema']
            item['defaults'] = bundle['resolved_preset']
            item['managed_fields'] = bundle['managed_fields']
        result.append(copy.deepcopy(item))
    return result
