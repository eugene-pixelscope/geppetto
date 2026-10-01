import tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from geppetto.storage import Store
from geppetto.service import Service
from geppetto.registry import preview,promote

class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.service=Service(Store(Path(self.tmp.name)/'db'),'catalog')
        with self.service.store.connect() as db:self.service.store.put(db,'run',{'id':'run','state':'succeeded','model_version':{'name':'model','version':'2','run_id':'ml-run'}})
    def tearDown(self):self.tmp.cleanup()
    def remote(self,previous=None):
        remote=Mock()
        def call(endpoint,body=None,params=None):
            if endpoint=='model-versions/get':return {'model_version':{'run_id':'ml-run','status':'READY'}}
            if endpoint=='registered-models/get':return {'registered_model':{'aliases':[] if previous is None else [{'alias':'candidate','version':previous}]}}
            if endpoint=='registered-models/alias':return {} if body else {'model_version':{'version':'2'}}
            raise AssertionError(endpoint)
        remote.call.side_effect=call;return remote
    def test_absent_alias_and_confirmed_audit(self):
        body={'run_id':'run','alias':'candidate'}
        with patch('geppetto.registry.client',side_effect=lambda:self.remote()):
            review=preview(self.service,body);self.assertIsNone(review['spec']['previous'])
            result=promote(self.service,{**body,'confirmation_hash':review['confirmation_hash']})
            self.assertEqual(result['state'],'confirmed')
    def test_changed_alias_requires_new_review(self):
        body={'run_id':'run','alias':'candidate'}
        with patch('geppetto.registry.client',return_value=self.remote('1')):review=preview(self.service,body)
        with patch('geppetto.registry.client',return_value=self.remote('3')):
            with self.assertRaises(ValueError):promote(self.service,{**body,'confirmation_hash':review['confirmation_hash']})
        self.assertFalse(self.service.list('registry_audit')['items'])
