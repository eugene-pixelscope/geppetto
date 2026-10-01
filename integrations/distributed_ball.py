"""torchrun worker: preserve Geppetto callbacks and publish only from global rank zero."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, '/opt/geppetto')
from integrations.model_runner import Recorder, ball, atomic_json


def main():
    import torch
    import mlflow
    payload = json.loads(Path(sys.argv[1]).read_text())
    torch.cuda.set_device(int(os.environ['LOCAL_RANK']))
    mlflow.set_tracking_uri(os.environ['MLFLOW_TRACKING_URI'])
    primary = int(os.environ['RANK']) == 0
    if payload.get('input_weights'):
        os.environ['GEPPETTO_INPUT_WEIGHTS'] = payload['input_weights']
    if torch.cuda.device_count() != int(os.environ['LOCAL_WORLD_SIZE']):
        raise ValueError('Visible GPUs differ from the distributed allocation')
    os.chdir('/cache')
    if primary:
        mlflow.start_run(run_id=payload['mlflow_run_id'])
    recorder = Recorder(Path(payload['output']), mlflow, payload['submission_id'])
    if not primary:
        recorder.record = lambda *args, **kwargs: None
    ball(payload['config'], Path(payload['root']), Path(payload['output']), recorder,
         payload['dataset'], payload.get('resume'))
    if primary:
        atomic_json(Path(payload['output'])/'distributed-result.json',
                    {'resume_epoch': getattr(recorder, 'resume_epoch', -1), 'resume_total': getattr(recorder, 'resume_total', 0)})


if __name__ == '__main__':
    main()
