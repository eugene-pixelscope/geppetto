import tempfile
import unittest
from pathlib import Path
from geppetto.adapters import sync_mlflow
from geppetto.service import Service
from geppetto.storage import Store
from geppetto.workshop import reconcile

class Client:
    def pages(self,*args):
        return [{'info':{'run_id':id,'start_time':1000,'status':'FINISHED'},'data':{}} for id in ['real','smoke']]
    def artifacts(self,*args):return []

class SelectedImportTest(unittest.TestCase):
    def test_allowlist_and_identity_survive_refresh(self):
        with tempfile.TemporaryDirectory() as home:
            service=Service(Store(Path(home)/'catalog.sqlite3'),mode='catalog')
            self.assertEqual(sync_mlflow(service,Client(),{'2':'ball-detection'},{'real'}),1)
            reconcile(service.store)
            original=service.get('run','mlflow-real')
            sync_mlflow(service,Client(),{'2':'ball-detection'},{'real'})
            reconcile(service.store)
            self.assertEqual(service.get('run','mlflow-real')['pinocchio_id'],original['pinocchio_id'])
            self.assertEqual(service.list('run')['total'],1)
            self.assertEqual(service.list('pinocchio')['total'],1)
            self.assertFalse(service.get('run','mlflow-real')['synthetic'])

    def test_sync_keeps_recorded_histories_when_upstream_omits_metrics(self):
        with tempfile.TemporaryDirectory() as home:
            service=Service(Store(Path(home)/'catalog.sqlite3'),mode='catalog')
            sync_mlflow(service,Client(),{'2':'ball-detection'},{'real'})
            with service.store.connect() as db:
                run=service.store.get(db,'run','mlflow-real')
                run['metrics']={'train/loss':[{'epoch':1,'step':0,'value':0.5}]}
                service.store.put(db,'run',run)
            sync_mlflow(service,Client(),{'2':'ball-detection'},{'real'})
            self.assertEqual(service.get('run','mlflow-real')['metrics'],
                             {'train/loss':[{'epoch':1,'step':0,'value':0.5}]})

    def test_real_importer_does_not_overwrite_custom_names(self):
        from tools.sync_real_ball import sync, RUNS
        from geppetto.workshop import rename
        class RealClient(Client):
            def pages(self,*args):
                experiment=args[2]['experiment_ids'][0]
                ids=list(RUNS)[:2] if experiment=='2' else list(RUNS)[2:]
                return [{'info':{'run_id':id,'start_time':1000,'status':'FINISHED'},'data':{}} for id in ids]
        with tempfile.TemporaryDirectory() as home:
            service=Service(Store(Path(home)/'catalog.sqlite3'),mode='catalog')
            sync(service,RealClient())
            run=service.get('run','mlflow-'+next(iter(RUNS)))
            pid=run['pinocchio_id'];rename(service,'pinocchio',pid,{'name':'내 공 모델'})
            sync(service,RealClient())
            self.assertEqual(service.get('pinocchio',pid)['name'],'내 공 모델')
            self.assertEqual(service.list('pinocchio')['total'],4)
            attempts=[service.get('run','mlflow-'+run_id) for run_id in list(RUNS)[3:]]
            self.assertEqual(len({run['pinocchio_id'] for run in attempts}),1)
            ablation_id=attempts[0]['pinocchio_id']
            self.assertEqual(service.get('pinocchio',ablation_id)['name'],'YOLO26x · one-to-many 절제')
            rename(service,'pinocchio',ablation_id,{'name':'YOLO26x · one-to-many'})
            for _ in range(2):
                sync(service,RealClient())
                self.assertEqual(service.get('pinocchio',ablation_id)['name'],'YOLO26x · one-to-many')
                self.assertTrue(service.get('pinocchio',ablation_id)['name_customized'])

    def test_deleted_model_is_not_reimported(self):
        from tools.sync_real_ball import sync, RUNS, ABLATION_ATTEMPTS
        from geppetto.model_history import change
        class RealClient(Client):
            def pages(self,*args):
                return [{'info':{'run_id':id,'start_time':1000,'status':'FINISHED'},'data':{}} for id in RUNS]
        with tempfile.TemporaryDirectory() as home:
            service=Service(Store(Path(home)/'catalog.sqlite3'),mode='catalog')
            sync(service,RealClient())
            run=service.get('run','mlflow-'+ABLATION_ATTEMPTS[0])
            pin=service.get('pinocchio',run['pinocchio_id'])
            change(service,pin['id'],{'name':pin['name']},{'id':'test','username':'test'})
            for _ in range(2):
                sync(service,RealClient())
                with self.assertRaises(KeyError):service.get('pinocchio',pin['id'])
                for id in ABLATION_ATTEMPTS:
                    with self.assertRaises(KeyError):service.get('run','mlflow-'+id)
                self.assertEqual(service.list('pinocchio')['total'],3)

    def test_verified_p2_epoch_history_uses_completed_epochs(self):
        from tools.sync_real_ball import sync, RUNS
        class EpochClient(Client):
            def pages(self,*args):
                experiment=args[2]['experiment_ids'][0]
                ids=list(RUNS)[:2] if experiment=='2' else list(RUNS)[2:]
                return [{'info':{'run_id':id,'start_time':1000,'status':'RUNNING'},'data':{'metrics':[{'key':'train/box_loss'}],'params':[{'key':'epochs','value':'245'}]}} for id in ids]
            def history(self,*args):return [{'step':18,'timestamp':2000,'value':.3}]
        with tempfile.TemporaryDirectory() as home:
            service=Service(Store(Path(home)/'catalog.sqlite3'),mode='catalog')
            sync(service,EpochClient())
            current=service.get('run','mlflow-'+list(RUNS)[2])
            self.assertEqual(current['progress']['epoch'],19)
            self.assertEqual(current['progress']['epoch_basis'],'completed')
            self.assertEqual(current['progress']['total'],245)
            self.assertEqual(current['metrics']['train/box_loss'][0]['epoch'],19)
            reference=service.get('run','mlflow-'+list(RUNS)[0])
            self.assertIsNone(reference['progress']['epoch'])
