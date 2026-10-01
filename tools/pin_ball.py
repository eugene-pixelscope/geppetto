"""Read-only integration manifest for the existing non-Git ball model tree."""
import hashlib
import json
from pathlib import Path

root=Path('/home/eugene/sports-ml/models/ball')
paths=['train.py','train_mlflow.py','pyproject.toml','configs/train/yolo26_p2_pose_baseline_split.yaml',
       'configs/models/yolo26-p2-pose.yaml','configs/data/soccer_ball_pose_split.yaml']
files={p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in paths}
# The vendored trainer is code too; skip data, checkpoints and caches.
for p in sorted((root/'ultralytics-src/ultralytics').rglob('*.py')):
    files[str(p.relative_to(root))]=hashlib.sha256(p.read_bytes()).hexdigest()
for p in sorted((root/'src').rglob('*.py')):
    files[str(p.relative_to(root))]=hashlib.sha256(p.read_bytes()).hexdigest()
sha=hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()
print(json.dumps({'code_version':'sha256:'+sha,'code_files':files,'git_commit':None},indent=2))
