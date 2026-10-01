"""Optional callbacks; importing this module does not import torch or Ultralytics."""
import math


def attach(model, run):
    """Attach to a YOLO instance in its actual trainer process before model.train().

    Finish and checkpoint upload remain explicit in the caller. Final validation's
    repeated on_fit_epoch_end is skipped to preserve immutable epoch metrics.
    """
    seen = set()

    def start(trainer):
        if run.enabled:
            run.progress(trainer.epoch + 1, trainer.epochs)

    def metrics(trainer):
        epoch = trainer.epoch + 1
        if not run.enabled or epoch in seen:
            return
        values = dict(trainer.metrics or {})
        values.update(trainer.label_loss_items(trainer.tloss, prefix='train'))
        values.update(getattr(trainer, 'lr', {}) or {})
        values = {str(key): float(value) for key, value in values.items() if math.isfinite(float(value))}
        if values:
            run.log(values, step=epoch, epoch=epoch)
        run.progress(epoch, trainer.epochs)
        seen.add(epoch)

    model.add_callback('on_train_epoch_start', start)
    model.add_callback('on_fit_epoch_end', metrics)
    return model
