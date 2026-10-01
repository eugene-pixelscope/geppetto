import sys
sys.path[:0]=['/opt/geppetto','/app/ball/ultralytics-src']
import torch
import numpy as np
from PIL import Image
from integrations.gradcam import activation_map,explain
from ultralytics import YOLO
torch.set_num_threads(2)
f=torch.tensor([[[[1.,2.],[3.,4.]],[[4.,3.],[2.,1.]]]])
g=torch.tensor([[[[2.,2.],[2.,2.]],[[-1.,-1.],[-1.,-1.]]]])
assert torch.equal(activation_map(f,g),torch.tensor([[[0.,1.],[4.,7.]]]))
assert not activation_map(f,torch.zeros_like(g)).any()
for name in ['yolo11n.yaml','yolo26n.yaml','yolo26n-pose.yaml']:
    model=YOLO(name).model.eval()
    image=Image.fromarray(np.random.default_rng(0).integers(0,255,(45,80,3),dtype=np.uint8))
    cam,meta=explain(model,image,64)
    assert cam.shape==(45,80) and np.isfinite(cam).all()
    assert meta['gradient_l1']>0 and meta['original_size']==[80,45]
    print(name,meta['branch'],meta['gradient_l1'],meta['feature_shape'],flush=True)
print('PASS: analytic Grad-CAM + Detect/Pose backward + letterbox dimensions')
