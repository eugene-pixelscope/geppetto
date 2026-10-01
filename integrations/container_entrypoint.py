"""Trusted image entrypoint; no shell evaluation of user configuration."""
import json
import os
from pathlib import Path
import sys

spec=json.loads(os.environ['GEPPETTO_SPEC_JSON'])
path=Path('/cache/submission.json')
path.write_text(json.dumps(spec))
if int(os.environ.get('GEPPETTO_NODE_RANK', '0')) > 0:
    import time
    import subprocess
    payload = Path(os.environ['GEPPETTO_OUTPUT'])/'distributed-input.json'
    deadline = time.monotonic() + 300
    while not payload.is_file():
        if time.monotonic() > deadline:
            raise TimeoutError('Primary node did not publish the distributed training input')
        time.sleep(1)
    os.environ['GEPPETTO_GPU_COUNT'] = str(spec['frozen']['gpu']['count'])
    os.environ['GEPPETTO_PREEMPTIBLE'] = '1' if spec['frozen'].get('preemptible') else '0'
    args = [sys.executable, '-m', 'torch.distributed.run', '--nnodes='+os.environ['GEPPETTO_NNODES'],
            '--nproc-per-node='+os.environ['GEPPETTO_GPU_COUNT'], '--node-rank='+os.environ['GEPPETTO_NODE_RANK'],
            '--rdzv-backend=c10d', '--rdzv-endpoint='+os.environ['GEPPETTO_MASTER'],
            '--rdzv-id='+os.environ['GEPPETTO_GROUP'], '--rdzv-conf=join_timeout=300,read_timeout=60',
            '/opt/geppetto/integrations/distributed_ball.py', str(payload)]
    os.execv(sys.executable, args)
os.execv(sys.executable,[sys.executable,'/opt/geppetto/integrations/model_runner.py',
    '--spec',str(path),'--model-root','/opt/model',
    '--dataset-registry',os.environ.get('GEPPETTO_DATASET_REGISTRY','/opt/geppetto/datasets.json'),
    '--output',os.environ.get('GEPPETTO_OUTPUT','/outputs')])
