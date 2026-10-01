import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import sys
import types
import unittest
from unittest.mock import Mock,patch

SPEC=importlib.util.spec_from_file_location('model_runner',Path(__file__).resolve().parents[1]/'integrations/model_runner.py')
runner=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def test_evaluation_uses_frozen_checkpoint_and_never_trains(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);weights=root/'best.pt';weights.write_bytes(b'evaluation-fixture')
            output=root/'out';output.mkdir()
            recorder=Mock();recorder.mlflow.artifacts.download_artifacts.return_value=str(weights)
            model=Mock();model.val.return_value=types.SimpleNamespace(results_dict={'mAP':0.0},speed={'inference':1.2})
            ultra=types.SimpleNamespace(YOLO=Mock(return_value=model),settings=Mock())
            policy={'checkpoint_uri':'mlflow-artifacts:/1/run/artifacts/best.pt','checkpoint_sha256':hashlib.sha256(weights.read_bytes()).hexdigest(),
                    'split':'test','confidence':.001,'iou':.7,'max_det':300,'precision':'fp32'}
            dataset={'root':'/datasets/ball','version':'v1','samples':[{'image':'images/one.jpg','label':'labels/one.txt','split':'test'}]}
            original=list(sys.path)
            try:
                with patch.dict(sys.modules,{'ultralytics':ultra}):
                    runner.ball_evaluate({'imgsz':1920,'batch':8},root,output,recorder,dataset,policy)
                    model.train.assert_not_called()
                    self.assertEqual(model.val.call_args.kwargs['split'],'test')
                    self.assertFalse(model.val.call_args.kwargs['half'])
                    recorder.record.assert_called_once()
                    self.assertEqual(recorder.record.call_args.args[0]['evaluation/mAP'],0)
                    weights.write_bytes(b'changed')
                    with self.assertRaisesRegex(ValueError,'checkpoint changed'):
                        runner.ball_evaluate({'imgsz':1920,'batch':8},root,output,recorder,dataset,policy)
            finally:sys.path[:]=original

    def test_partial_checkpoint_never_published(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'best.pt').write_bytes(b'incomplete')
            mlflow=Mock()
            recorder=runner.Recorder(root,mlflow,'id')
            recorder.finish('failed','ml-run')
            result=json.loads((root/'result.json').read_text())
            self.assertFalse(any(a['path'].endswith('.pt') for a in result['artifacts']))

    def test_finalized_checkpoint_role_and_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            source=root/'best.pt'
            source.write_bytes(b'complete-fixture')
            runner.finalize_checkpoint(source,root)
            recorder=runner.Recorder(root,Mock(),'id')
            recorder.finish('succeeded','ml-run')
            result=json.loads((root/'result.json').read_text())
            self.assertEqual(result['artifacts'][0]['path'],'completed/best.pt')
            self.assertFalse(result['artifacts'][0]['resume'])

    def test_nonfinite_metrics_not_published(self):
        with tempfile.TemporaryDirectory() as folder:
            mlflow=Mock()
            recorder=runner.Recorder(folder,mlflow,'id')
            recorder.record({'zero':0,'missing':None,'nan':float('nan')},1)
            sent=mlflow.log_metrics.call_args.args[0]
            self.assertEqual(sent['zero'],0)
            self.assertNotIn('nan',sent)
            self.assertNotIn('missing',sent)

    def test_mlflow_metric_names_and_root_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            mlflow=Mock();recorder=runner.Recorder(folder,mlflow,'id')
            recorder.record({'metrics/precision(B)':.5,'metrics/precision_B_':.7},1)
            sent=mlflow.log_metrics.call_args.args[0]
            self.assertEqual(len([k for k in sent if k.startswith("metrics/")]),2)
            self.assertTrue(all('(' not in key for key in sent))
            self.assertIn('metrics/precision(B)',recorder.metrics)
            recorder.finish('failed','ml-run')
            self.assertTrue(all(c.kwargs['artifact_path']!='.' for c in mlflow.log_artifact.call_args_list))

    def test_source_drift_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'config.py').write_text('changed')
            with self.assertRaises(ValueError):
                runner.verify_code(root,{'config.py':'0'*64})

    def test_ball_uses_assigned_device_before_runtime_resolution(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            source=root/'configs/train/yolo26_p2_pose_baseline_split.yaml'
            source.parent.mkdir(parents=True)
            source.write_text('model: model.yaml\ndevice: auto\nscale_batch_by_gpus: true\n')
            output=root/'out'
            output.mkdir()
            best=output/'best.pt'
            best.write_bytes(b'fixture')
            trainer=Mock()
            trainer.best=best
            yolo=Mock()
            yolo.trainer=trainer
            runtime=Mock(side_effect=lambda cfg,path:dict(cfg))
            train=types.SimpleNamespace(resolve_runtime=runtime,validate_training_config=Mock())
            ultralytics=types.SimpleNamespace(YOLO=Mock(return_value=yolo),settings=Mock())
            original=list(sys.path)
            try:
                with patch.dict(sys.modules,{'train':train,'ultralytics':ultralytics}):
                    runner.ball({'epochs':2,'batch':1,'imgsz':64,'patience':0},root,output,Mock(),
                        {'root':'/datasets/input','samples':[
                            {'image':'images/a.png','label':'labels/a.txt','split':'train'},
                            {'image':'images/b.png','label':'labels/b.txt','split':'validation'}]})
            finally:
                sys.path[:]=original
            self.assertEqual(runtime.call_args.args[0]['device'],'0')
            self.assertFalse(runtime.call_args.args[0]['scale_batch_by_gpus'])
            self.assertEqual(yolo.train.call_args.kwargs['device'],'0')
