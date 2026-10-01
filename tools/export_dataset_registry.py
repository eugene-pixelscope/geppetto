"""Export a verified dataset manifest for a read-only dataset PVC."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from geppetto.dataset_versions import export_registry
from geppetto.service import Service
from geppetto.storage import Store

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--database',type=Path,required=True)
parser.add_argument('--dataset',required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--container-root',required=True,help='Example: /datasets/soccer/ball-v1')
args=parser.parse_args()
export_registry(Service(Store(args.database),'readonly'),args.dataset,args.output,args.container_root)
print(args.output)
