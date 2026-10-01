"""Integration scaffold: replace train_one_epoch with your actual training code.

This module does not start training when imported and does not create mock data.
"""
import geppetto_sdk as geppetto


def track_training(train_one_epoch, *, epochs, architecture, config, checkpoint, dataset_id, node_id):
    with geppetto.init(project='project-ball-detection', name=architecture,
                       architecture=architecture, config=config,
                       dataset_id=dataset_id, node_id=node_id) as run:
        print('Geppetto run:', run.id)  # Preserve this ID for recovery.
        for epoch in range(1, epochs + 1):
            run.progress(epoch, epochs)
            metrics = train_one_epoch(epoch)  # e.g. {'train/loss': loss, 'metrics/mAP50(B)': mAP}
            run.log(metrics, step=epoch, epoch=epoch)
        run.log_artifact(checkpoint, role='inference_weights')
