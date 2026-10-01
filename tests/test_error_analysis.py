import copy
import unittest
from pathlib import Path
import tempfile
from fastapi.testclient import TestClient
from functools import partial
from geppetto.app import create_app as _create_app
# These isolated fixtures exercise catalog behavior without browser authentication.
create_app = partial(_create_app, auth_required=False)
from geppetto.error_analysis import analyze, matches


class MatchingTests(unittest.TestCase):
    def report(self):
        return {'id':'x','prediction_floor':0,'records':[
            {'index':0,'camera':'A','image':'a.jpg','width':1000,'height':1000,
             'truth':[[.5,.5,.01,.01],[.8,.8,.1,.1]],'predictions':[
                 {'box':[.5,.5,.01,.01],'score':.9},{'box':[.5,.5,.01,.01],'score':.8}]},
            {'index':1,'camera':'B','image':'b.jpg','width':1000,'height':1000,'truth':[],
             'predictions':[{'box':[.1,.1,.1,.1],'score':.7}]},
            {'index':2,'camera':'B','image':'c.jpg','width':1000,'height':1000,'truth':[], 'predictions':[]}]}

    def test_one_to_one_duplicates_negatives_and_empty_images(self):
        result=analyze(self.report())
        self.assertEqual({k:result['totals'][k] for k in ('tp','fn','fp','images')},{'tp':1,'fn':1,'fp':2,'images':3})
        self.assertEqual(result['totals']['recall'],.5)
        self.assertEqual(result['totals']['precision'],1/3)
        self.assertEqual(sum(c['fn'] for c in result['cells']),1)
        self.assertEqual(sum(c['fp'] for c in result['cells']),2)
        self.assertEqual(result['sizes']['<16 px'],{'truth':1,'tp':1})
        self.assertEqual(result['failure_count'],1)
        self.assertIsNone(result['cells'][7]['fn_rate'])

    def test_heatmap_uses_all_selected_camera_observations(self):
        result=analyze(self.report(),camera='B',kind='fp')
        heat=result['heatmap']
        self.assertEqual(len(heat['fp']),128*72)
        self.assertEqual(sum(heat['fp']),result['totals']['fp'])
        self.assertEqual(sum(heat['truth']),result['totals']['truth'])
        self.assertEqual(heat['background']['index'],1)
        self.assertEqual(sum(analyze(self.report(),confidence=1)['heatmap']['fp']),0)
        self.assertEqual(sum(analyze(self.report(),kind='fp',cell=63)['heatmap']['fp']),2)

    def test_threshold_camera_cell_and_zero_denominators(self):
        self.assertEqual(analyze(self.report(),confidence=.85)['totals']['fp'],0)
        self.assertEqual(analyze(self.report(),camera='B',kind='fp',cell=0)['failure_count'],1)
        result=analyze(self.report(),confidence=1,camera='B')
        self.assertIsNone(result['totals']['recall'])
        self.assertIsNone(result['totals']['precision'])
        report=self.report();report['prediction_floor']=.6
        with self.assertRaises(ValueError):analyze(report,.5)
        with self.assertRaises(ValueError):analyze(report,overlap=0)
        with self.assertRaises(ValueError):analyze(report,confidence=float('nan'))

    def test_low_iou_counts_as_both_miss_and_false_positive(self):
        truth=[[.5,.5,.1,.1]]
        self.assertEqual(matches(truth,[{'box':[.9,.9,.1,.1],'score':.9}],.5),(set(),set()))

    def test_failure_sorting_and_geometry(self):
        report=self.report()
        report['records'][0]['predictions'].append({'box':[.2,.2,.03,.03],'score':.55})
        result=analyze(report,kind='fp',sort='errors')
        self.assertEqual([r['index'] for r in result['failures']],[0,1])
        self.assertEqual(result['failures'][0]['selected_errors'],2)
        self.assertEqual(result['failures'][0]['width'],1000)
        report['records'][1]['predictions'][0]['score']=.99
        self.assertEqual(analyze(report,kind='fp',sort='confidence')['failures'][0]['index'],1)
        self.assertEqual(analyze(report,kind='fp',sort='index')['failures'][0]['index'],0)
        with self.assertRaises(ValueError):analyze(report,sort='invalid')


class AnalysisAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=create_app(Path(self.tmp.name),'demo',start_worker=False)
        self.service=self.app.state.service
        self.client=TestClient(self.app)
        with self.service.store.connect() as db:
            self.run=next(r for r in self.service.store.all(db,'run') if r['category']=='ball-detection')
        self.dataset=self.service.get('dataset','demo-ball-detection')
        self.base='/api/runs/'+self.run['id']+'/error-analysis'
        self.headers={'X-Geppetto-Request':'1'}

    def tearDown(self):
        self.client.close();self.tmp.cleanup()

    def body(self):
        return {'dataset_id':self.dataset['id'],'dataset_version':self.dataset['version'],
                'split':'validation','checkpoint':'fixture','prediction_floor':0,
                'images':[{'index':4,'predictions':[]},{'index':5,'predictions':[]}]}

    def test_import_empty_predictions_and_pinned_image(self):
        self.assertFalse(self.client.get(self.base).json()['available'])
        result=self.client.post(self.base,json=self.body(),headers=self.headers)
        self.assertEqual(result.status_code,200,result.text)
        report=self.client.get(self.base).json()
        self.assertEqual(report['totals']['fn'],2)
        self.assertEqual(report['coverage'],2)
        self.assertTrue(report['report']['synthetic'])
        url=self.base+'/'+result.json()['id']+'/images/4'
        self.assertEqual(self.client.get(url).status_code,200)
        (Path(self.dataset['root'])/'4.png').write_bytes(b'changed')
        self.assertEqual(self.client.get(url).status_code,409)

    def test_reject_wrong_version_split_duplicates_and_missing_predictions(self):
        mutations=[{'dataset_version':'wrong'},{'split':'train'},
                   {'images':[{'index':4}]},{'images':[{'index':0,'predictions':[]}]},
                   {'images':[{'index':4,'predictions':[]}]*2},
                   {'images':[{'index':4,'predictions':[{'box':[.1,.1,0,.1],'score':.9}]}]}]
        for change in mutations:
            body={**self.body(),**change}
            self.assertEqual(self.client.post(self.base,json=body,headers=self.headers).status_code,422,change)

    def test_demo_is_explicit_and_reports_are_scoped(self):
        r=self.client.post(self.base+'/demo',json={},headers=self.headers)
        self.assertEqual(r.status_code,200,r.text)
        report=self.client.get(self.base).json()
        self.assertIn('SYNTHETIC',report['report']['checkpoint'])
        self.assertEqual(report['totals']['tp'],1)
        self.assertEqual(report['totals']['fp'],2)
        self.assertEqual(self.client.get(self.base+'?report_id=another').status_code,404)

    def test_real_yolo_labels_and_missing_label_are_distinct(self):
        import json
        d=copy.deepcopy(self.dataset)
        d.update(id='real-fixture',synthetic=False,policy={'format':'yolo-pose'})
        root=Path(d['root'])
        for i in (4,5):
            name=f'{i}.txt'
            (root/name).write_text('0 0.5 0.5 0.1 0.1 0.5 0.5 2\n' if i==4 else '')
            d['samples'][i]['label']=name
            d['samples'][i].pop('label_file',None)
        r=copy.deepcopy(self.run);r['synthetic']=False
        r['frozen']['datasets']=[{'id':d['id'],'version':d['version']}]
        with self.service.store.connect() as db:
            self.service.store.put(db,'dataset',d);self.service.store.put(db,'run',r)
        body={**self.body(),'dataset_id':d['id']}
        response=self.client.post(self.base,json=body,headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        report=self.client.get(self.base).json()
        self.assertFalse(report['report']['synthetic'])
        self.assertEqual(report['totals']['truth'],1)
        self.assertEqual(report['totals']['images'],2)
        (root/'5.txt').unlink()
        # No missing label may be silently converted to a negative image.
        from geppetto.error_analysis import import_evaluation
        with self.assertRaisesRegex(ValueError,'ground truth label'):import_evaluation(self.service,r['id'],body)

    def test_evaluation_pins_dataset_without_rewriting_imported_training(self):
        run=copy.deepcopy(self.run);run['frozen']['datasets']=None
        with self.service.store.connect() as db:self.service.store.put(db,'run',run)
        r=self.client.post(self.base,json=self.body(),headers=self.headers)
        self.assertEqual(r.status_code,200,r.text)
        report=self.client.get(self.base).json()['report']
        self.assertEqual(report['dataset_version'],self.dataset['version'])
        self.assertFalse(report['training_dataset_match'])
        self.assertIsNone(self.service.get('run',run['id'])['frozen']['datasets'])
