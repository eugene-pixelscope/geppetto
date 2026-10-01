"""Actual Grad-CAM for reviewed Ultralytics Detect/Pose heads (YOLO11/26).

Class logit -> detection head input feature map, before non-differentiable NMS.
https://arxiv.org/abs/1610.02391
"""
import hashlib
import json
from pathlib import Path
import sys


def verify_sample(dataset, policy):
    from geppetto.files import fingerprint
    sample = policy['sample']
    index = sample['index']
    registered = dataset['samples'][index]
    if any(registered[k] != sample[k] for k in ('image', 'split', 'image_file')):
        raise ValueError('Grad-CAM sample differs from the pinned registry version')
    if fingerprint(dataset['root'], sample['image']) != sample['image_file']:
        raise ValueError('Grad-CAM image content changed')


def activation_map(feature, gradient):
    """Keep unnormalized signal for validation; normalize only for display."""
    import torch
    if not torch.isfinite(feature).all() or not torch.isfinite(gradient).all():
        raise ValueError('Non-finite Grad-CAM activation or gradient')
    return (feature * gradient.mean(dim=(-2, -1), keepdim=True)).sum(1).relu()


def explain(network, image, size, class_id=0, rank=1, iou=.7):
    import numpy as np
    import torch
    import torch.nn.functional as F
    from torchvision.ops import nms
    from PIL import Image
    network.eval().float()
    head = network.model[-1]
    if not hasattr(head, 'forward_head') or not hasattr(head, '_get_decode_boxes'):
        raise ValueError('Unsupported detection head: Grad-CAM requires the reviewed YOLO11/26 head')
    if not 0 <= class_id < head.nc:
        raise ValueError('Target class does not exist in this checkpoint')
    device = next(network.parameters()).device
    stride = int(head.stride.max().item())
    if size % stride:
        raise ValueError('Input size must align to model stride')
    w, h = image.size
    ratio = min(size/w, size/h)
    rw, rh = round(w*ratio), round(h*ratio)
    left, top = (size-rw)//2, (size-rh)//2
    canvas = Image.new('RGB', (size, size), (114, 114, 114))
    canvas.paste(image.resize((rw, rh), Image.Resampling.BILINEAR), (left, top))
    tensor = torch.from_numpy(np.asarray(canvas).copy()).permute(2, 0, 1).unsqueeze(0).to(device=device, dtype=torch.float32).div_(255).requires_grad_(True)
    # Direct module call: predict() uses inference_mode and would discard gradients.
    network.requires_grad_(False)
    with torch.enable_grad():
        output = network(tensor)
        raw = output[1] if isinstance(output, tuple) else output
        if isinstance(raw, tuple):  # legacy pose wrapper
            raw = raw[0]
        if not isinstance(raw, dict):
            raise ValueError('Unsupported raw prediction format; no surrogate map generated')
        branch = 'one2one' if head.end2end else 'one2many'
        preds = raw['one2one'] if head.end2end else raw
        logits, feats = preds['scores'][0, class_id], preds['feats']
        boxes = head._get_decode_boxes(preds)[0].T.detach()
        if not head.end2end and not getattr(head, 'xyxy', False):
            center, wh = boxes[:, :2].clone(), boxes[:, 2:].clone()
            boxes = torch.cat((center-wh/2, center+wh/2), 1)
        probabilities = logits.detach().sigmoid()
        # Top 3000 candidates bound NMS cost. End-to-end heads already learn one-to-one.
        candidates = probabilities.argsort(descending=True)[:3000]
        if not head.end2end:
            candidates = candidates[nms(boxes[candidates], probabilities[candidates], iou)]
        if rank > len(candidates):
            raise ValueError('Requested detection rank is unavailable')
        anchor = int(candidates[rank-1])
        offset, level = 0, None
        for i, feature in enumerate(feats):
            count = feature.shape[-2]*feature.shape[-1]
            if offset <= anchor < offset+count:
                level = i
                break
            offset += count
        if level is None:
            raise ValueError('Prediction/feature alignment mismatch')
        feature = feats[level]
        gradient, = torch.autograd.grad(logits[anchor], feature)
        cam = activation_map(feature, gradient)[0]
        peak = float(cam.max())
        signal = peak > 0
        normalized = cam/peak if signal else torch.zeros_like(cam)
        full = F.interpolate(normalized[None, None], size=(size, size), mode='bilinear', align_corners=False)[0, 0]
        cropped = full[top:top+rh, left:left+rw]
        mapped = F.interpolate(cropped[None, None], size=(h, w), mode='bilinear', align_corners=False)[0, 0].detach().cpu().numpy()
        box = boxes[anchor].cpu().numpy()
        box[[0, 2]] = (box[[0, 2]]-left)/(rw/w)
        box[[1, 3]] = (box[[1, 3]]-top)/(rh/h)
        box[[0, 2]] = box[[0, 2]].clip(0, w)
        box[[1, 3]] = box[[1, 3]].clip(0, h)
        metadata = {'method': 'Grad-CAM', 'target': 'pre-NMS class logit', 'class_id': class_id, 'class_name': str(network.names[class_id]), 'rank': rank, 'anchor': anchor, 'branch': branch, 'score': float(probabilities[anchor]), 'box_xyxy': box.tolist(), 'layer': f'detection head input {level} (stride {int(head.stride[level])})', 'feature_shape': list(feature.shape), 'gradient_l1': float(gradient.abs().sum()), 'raw_cam_max': peak, 'has_positive_signal': signal, 'imgsz': size, 'original_size': [w, h], 'letterbox': {'resized': [rw, rh], 'left': left, 'top': top}, 'normalization': 'per-image positive maximum', 'selection': 'top score, one-to-one head' if head.end2end else f'top score after NMS (IoU {iou})', 'candidate_limit': 3000}
    return mapped, metadata


def run(config, root, output, recorder, dataset, policy):
    import numpy as np
    from PIL import Image
    from geppetto.files import open_beneath
    verify_sample(dataset, policy)
    sys.path[:0] = [str(root), str(root/'src'), str(root/'ultralytics-src')]
    from ultralytics import YOLO, settings
    settings.update({'mlflow': False, 'wandb': False})
    checkpoint = Path(recorder.mlflow.artifacts.download_artifacts(artifact_uri=policy['checkpoint_uri'], dst_path=str(output/'checkpoint')))
    with checkpoint.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != policy['checkpoint_sha256']:
            raise ValueError('Grad-CAM checkpoint content changed')
    with open_beneath(dataset['root'], policy['sample']['image']) as stream:
        # RGB throughout; no OpenCV BGR conversion in the display path.
        image = Image.open(stream).convert('RGB')
    model = YOLO(str(checkpoint)).model.to('cuda:0')
    cam, metadata = explain(model, image, config['imgsz'], policy['class_id'], policy['target_rank'], policy['nms_iou'])
    image.save(output/'gradcam-source.png')
    # Transparent at zero; sequential yellow -> red denotes positive contribution.
    rgb = np.empty((*cam.shape, 4), dtype=np.uint8)
    rgb[..., 0] = 255
    rgb[..., 1] = ((1-cam)*230).astype(np.uint8)
    rgb[..., 2] = 0
    rgb[..., 3] = (cam*255).astype(np.uint8)
    heat = Image.fromarray(rgb, 'RGBA')
    heat.save(output/'gradcam-heatmap.png')
    overlay = heat.copy()
    overlay.putalpha(Image.fromarray((cam*180).astype(np.uint8)))
    Image.alpha_composite(image.convert('RGBA'), overlay).convert('RGB').save(output/'gradcam-overlay.png')
    np.save(output/'gradcam-values.npy', cam, allow_pickle=False)
    metadata.update(checkpoint_sha256=policy['checkpoint_sha256'], dataset_version=dataset['version'], sample=policy['sample'])
    (output/'gradcam.json').write_text(json.dumps(metadata, ensure_ascii=False, allow_nan=False))
    recorder.record({'gradcam/target_score': metadata['score'], 'gradcam/gradient_l1': metadata['gradient_l1'], 'gradcam/positive_signal': int(metadata['has_positive_signal'])}, step=0, phase='Grad-CAM')
