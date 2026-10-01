import tempfile
import unittest
from pathlib import Path
from geppetto.storage import Store
from geppetto.service import Service
from geppetto.visualizations import publish
from geppetto.sweeps import control,tpe_next

class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'db');self.service=Service(self.store)
        with self.store.connect() as db:self.store.put(db,'run',{'id':'run'})
    def tearDown(self):self.tmp.cleanup()
    def test_histogram_replay_and_conflict(self):
        body={'type':'histogram','name':'weights','step':0,'data':{'edges':[0,1,2],'counts':[0,5]}}
        self.assertEqual(publish(self.service,'run',body),publish(self.service,'run',body))
        body['data']['counts'][0]=1
        with self.assertRaises(ValueError):publish(self.service,'run',body)
    def test_invalid_diagnostics(self):
        for kind,data in [('histogram',{'edges':[1,0],'counts':[1]}),('embedding',{'projection':'pca','points':[{'x':float('nan'),'y':0}]}),('trace',{'events':[{'name':'x','start_us':0,'duration_us':-1}]})]:
            with self.assertRaises(ValueError):publish(self.service,'run',{'type':kind,'name':'x','step':0,'data':data})
    def test_budget_stops_active_and_queued(self):
        runs=[{'id':str(i),'sweep_id':'s','state':'running' if i==0 else 'queued','frozen':{'gpu':{'count':1}},'metrics':{}} for i in range(2)]
        runs[0]['dispatch']={'created_at':0};runs[0]['started_at']=0
        sweep={'id':'s','state':'active','limits':{'gpu_seconds':10}}
        with self.store.connect() as db:
            self.store.put(db,'sweep',sweep)
            for r in runs:self.store.put(db,'run',r)
            control(self.store,db,runs,now=20)
            self.assertEqual(self.store.get(db,'sweep','s')['state'],'budget_exhausted')
            self.assertTrue(all(self.store.get(db,'run',r['id'])['stop_requested_at']==20 for r in runs))
    def test_pruning_uses_direction_and_requires_peers(self):
        runs=[{'id':str(i),'sweep_id':'s','state':'running','metrics':{'loss':[{'step':1,'value':i+1}]}} for i in range(3)]
        with self.store.connect() as db:
            self.store.put(db,'sweep',{'id':'s','state':'active','metric':'loss','direction':'minimize','pruning':{'min_step':1,'max_step':2,'reduction_factor':3}})
            control(self.store,db,runs,now=20)
        self.assertNotIn('stop_requested_at',runs[0]);self.assertEqual(runs[1]['pruned_at_step'],1)
    def test_tpe_prefers_good_categorical_choice(self):
        runs=[{'id':str(i),'sweep_id':'s','state':'succeeded' if i<4 else 'queued','created_at':i,'metrics':{'score':[{'value':10 if i==0 else 0}]}} for i in range(6)]
        sweep={'id':'s','direction':'maximize','metric':'score','trials':[{'run_id':str(i),'parameters':{'batch':8 if i in (0,5) else 16}} for i in range(6)]}
        self.assertEqual(tpe_next(sweep,runs),'5')
