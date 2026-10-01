import copy
import unittest
from unittest.mock import patch
import test_platform as platform
from test_platform import OP
from geppetto.configuration import digest
from integrations.gradcam import verify_sample


class GradcamTests(unittest.TestCase):
    setUp = platform.PlatformTests.setUp
    tearDown = platform.PlatformTests.tearDown
    post = platform.PlatformTests.post
    launch = platform.PlatformTests.launch

    def source(self):
        r=self.launch('source')
        r.update(state='succeeded', template_id=None, node_id='imported-other-node', imported=True,
                 artifacts=[{'id':'weights','role':'inference_weights','path':'weights/best.pt','artifact_uri':'mlflow-artifacts:/1/run/artifacts','complete':True,'size':8}])
        with self.service.store.connect() as db:self.service.store.put(db,'run',r)
        return r

    def body(self):
        return {'profile_id':self.profile['id'],'dataset_id':self.dataset['id'],'checkpoint_id':'weights','index':0,'imgsz':960,'target_rank':1}

    @patch('geppetto.remote_files.fetch_completed', return_value=('/cache','weights.pt',{'sha256':'b'*64,'size':8}))
    def test_imported_source_pinned_agent_job_idempotency(self, fetch):
        r=self.source();base='/api/runs/'+r['id']+'/gradcam'
        preview=self.post(base+'/preview',self.body())
        frozen=preview['frozen'];self.assertEqual(frozen['parent_id'],r['id']);self.assertEqual(frozen['node_id'],self.node['id'])
        self.assertEqual(frozen['evaluation']['checkpoint_sha256'],'b'*64)
        self.assertEqual(frozen['evaluation']['sample']['image_file'],self.dataset['samples'][0]['image_file'])
        body={**self.body(),'confirmation_hash':preview['confirmation_hash']}
        one=self.post(base,body,{**OP,'Idempotency-Key':'gradcam-repeat'})
        two=self.post(base,body,{**OP,'Idempotency-Key':'gradcam-repeat'})
        self.assertEqual(one['id'],two['id']);self.assertEqual(one['pinocchio_id'],r['pinocchio_id'])
        self.assertEqual(one['source_kind'],'gradcam');self.assertEqual(fetch.call_count,1)
        self.assertEqual(len(self.client.get(base,headers=OP).json()['runs']),1)
        body['imgsz']=640
        self.assertEqual(self.client.post(base,json=body,headers={**OP,'Idempotency-Key':'gradcam-changed'}).status_code,422)

    @patch('geppetto.remote_files.fetch_completed', return_value=('/cache','weights.pt',{'sha256':'b'*64,'size':8}))
    def test_changed_image_and_archived_model_rejected(self, fetch):
        r=self.source();base='/api/runs/'+r['id']+'/gradcam'
        preview=self.post(base+'/preview',self.body())
        (self.home/'data/image').write_bytes(b'changed')
        self.assertEqual(self.client.post(base+'/preview',json=self.body(),headers=OP).status_code,422)
        with self.service.store.connect() as db:self.service.store.put(db,'model_history',{'id':r['pinocchio_id'],'deleted_at':1})
        self.assertEqual(self.client.post(base+'/preview',json=self.body(),headers=OP).status_code,409)

    def test_runner_checks_selected_sample_only_and_rejects_tampering(self):
        d=copy.deepcopy(self.dataset);d['samples'].append({'image':'missing-not-selected'})
        p={'sample':{'index':0,**d['samples'][0]}}
        verify_sample(d,p)
        bad=copy.deepcopy(p);bad['sample']['image']='../elsewhere'
        with self.assertRaises(ValueError):verify_sample(d,bad)
        (self.home/'data/image').write_bytes(b'changed')
        with self.assertRaises(ValueError):verify_sample(d,p)
