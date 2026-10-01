"""Call from the actual trainer process; no training runs on import."""
import geppetto_sdk as geppetto
from geppetto_sdk.ultralytics import attach


def train_yolo(model, *, dataset_yaml, dataset_id, node_id, architecture='YOLO26 P2', epochs=245, imgsz=1920, batch=16, device=0):
    # model is an existing YOLO instance, including a custom-layer implementation.
    config = dict(data=dataset_yaml, epochs=epochs, imgsz=imgsz, batch=batch, device=device)
    with geppetto.init(project='project-ball-detection', name=architecture, architecture=architecture,
                       config=config, dataset_id=dataset_id, node_id=node_id) as run:
        print('Geppetto run:', run.id)
        attach(model, run)
        model.train(**config)
        if run.enabled:
            run.log_artifact(model.trainer.best, role='inference_weights')
    return run.id
