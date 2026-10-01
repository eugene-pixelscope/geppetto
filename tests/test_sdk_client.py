import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import URLError
import uvicorn
from functools import partial
from geppetto.app import create_app as _create_app
# These isolated fixtures exercise catalog behavior without browser authentication.
create_app = partial(_create_app, auth_required=False)
from geppetto.tracking import issue
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'sdk'))
import geppetto_sdk as sdk
from geppetto_sdk.ultralytics import attach as attach_yolo

class SDKClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=create_app(self.tmp.name,'catalog',False,token='operator')
        self.service=self.app.state.service
        self.token=issue(self.service,{'project_id':'project-ball-detection'})['token']
        self.socket=socket.socket();self.socket.bind(('127.0.0.1',0))
        self.url='http://127.0.0.1:'+str(self.socket.getsockname()[1])
        self.server=uvicorn.Server(uvicorn.Config(self.app,log_level='error'))
        self.thread=threading.Thread(target=self.server.run,kwargs={'sockets':[self.socket]},daemon=True);self.thread.start()
        for _ in range(100):
            if self.server.started:break
            time.sleep(.01)
        self.assertTrue(self.server.started)
        self.kw=dict(url=self.url,api_key=self.token,heartbeat_interval=0)
    def tearDown(self):
        self.server.should_exit=True;self.thread.join(timeout=5);self.socket.close();self.tmp.cleanup()
    def test_real_transport_lifecycle_artifact_and_attach(self):
        with sdk.init(**self.kw,name='SDK e2e') as run:
            run.log({'loss':1},epoch=1)
            again=sdk.attach(run.id,**self.kw);again.log({'loss':.5},epoch=2)
            run.progress(2,2)
            artifact=Path(self.tmp.name)/'weights.bin';artifact.write_bytes(b'0123456789'*150000)
            self.assertTrue(run.log_artifact(artifact)['complete'])
        record=self.service.get('run',run.id)
        self.assertEqual(record['state'],'succeeded');self.assertEqual([p['step'] for p in record['metrics']['loss']],[0,1])
        self.assertEqual(record['artifacts'][0]['size'],1500000)
    def test_response_loss_retry_does_not_duplicate(self):
        original=sdk._Client.post
        def lossy(client,path,body):
            opener=client.opener.open;lost=False
            def open_once(request,**kwargs):
                nonlocal lost
                result=opener(request,**kwargs)
                if not lost:
                    lost=True;result.read();result.close();raise URLError('response lost after commit')
                return result
            with patch.object(client.opener,'open',side_effect=open_once):return original(client,path,body)
        with patch.object(sdk._Client,'post',lossy):
            with sdk.init(**self.kw) as run:run.log({'loss':1})
        self.assertEqual(self.service.list('run')['total'],1)
        self.assertEqual(len(self.service.get('run',run.id)['metrics']['loss']),1)
    def test_exception_and_heartbeat(self):
        kw={**self.kw,'heartbeat_interval':.05}
        with self.assertRaisesRegex(RuntimeError,'training failed'):
            with sdk.init(**kw) as run:
                original=self.service.get('run',run.id)['observed_at'];time.sleep(.15)
                self.assertGreater(self.service.get('run',run.id)['observed_at'],original)
                raise RuntimeError('training failed')
        self.assertEqual(self.service.get('run',run.id)['state'],'failed');self.assertTrue(run._stop.is_set())

    def test_typed_evaluation_real_transport_and_idempotent_import(self):
        root=Path(self.tmp.name)/'samples';root.mkdir();(root/'one').write_bytes(b'example')
        self.service.roots=[str(root)]
        dataset=self.service.register_dataset({'name':'evaluation data','category':'ball-detection','root':str(root),
            'samples':[{'image':'one','split':'test'}]})
        with sdk.init(**self.kw,dataset_id=dataset['id']) as run:
            kwargs={'dataset_id':dataset['id'],'type':'keypoints',
                    'policy':{'evaluator_version':'v1','split':'test','checkpoint_sha256':'a'*64,'coordinate_unit':'normalized','tolerance':.1},
                    'samples':[{'index':0,'target':[.5,.5],'prediction':[.5,.5]}]}
            result=run.log_evaluation(**kwargs)
            self.assertEqual(run.log_evaluation(**kwargs)['id'],result['id'])
        self.assertEqual(self.service.list('analysis_report')['total'],1)
    def test_rank_zero_and_transport_constraints(self):
        with patch.dict(os.environ,{'RANK':'1'}):
            with sdk.init() as run:
                self.assertFalse(run.enabled);run.log({'loss':1});run.log_artifact('/does/not/exist')
        self.assertEqual(self.service.list('run')['total'],0)
        with self.assertRaises(ValueError):sdk.init(url='http://example.com',api_key='x')
    def test_yolo_final_validation_duplicate(self):
        callbacks={};model=SimpleNamespace(add_callback=lambda name,fn:callbacks.update({name:fn}))
        trainer=SimpleNamespace(epoch=0,epochs=2,metrics={'metrics/mAP50(B)':.5},tloss=None,lr={'lr/pg0':.001},label_loss_items=lambda loss,prefix:{prefix+'/loss':.8})
        with sdk.init(**self.kw) as run:
            attach_yolo(model,run);callbacks['on_train_epoch_start'](trainer);callbacks['on_fit_epoch_end'](trainer)
            trainer.metrics={'metrics/mAP50(B)':.9};callbacks['on_fit_epoch_end'](trainer)
        metrics=self.service.get('run',run.id)['metrics'];self.assertEqual(len(metrics['train/loss']),1);self.assertEqual(metrics['metrics/mAP50(B)'][0]['value'],.5)
