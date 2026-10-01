from pathlib import Path
import tempfile
import unittest
from fastapi.testclient import TestClient
from functools import partial
from geppetto.app import create_app as _create_app
# These isolated fixtures exercise catalog behavior without browser authentication.
create_app = partial(_create_app, auth_required=False)
from geppetto.indexed_datasets import import_yolo_splits, sample_page, sample_at


class IndexedDatasetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.home=Path(self.tmp.name)
        self.app=create_app(self.home/'app','demo',start_worker=False)
        self.service=self.app.state.service
        self.client=TestClient(self.app)
        self.root=self.home/'source'
        (self.root/'images').mkdir(parents=True)
        (self.root/'labels').mkdir()
        self.splits={}
        for split,count in [('train',7),('validation',2),('test',1)]:
            paths=[]
            for i in range(count):
                image=self.root/'images'/f'{split}-{i}.jpg'
                image.write_bytes(b'image fixture')
                (self.root/'labels'/f'{split}-{i}.txt').write_text('0 0.5 0.5 0.2 0.2 0.5 0.5 2\n')
                paths.append(str(image))
            file=self.home/(split+'.txt')
            file.write_text('\n'.join(paths)+'\n')
            self.splits[split]=file

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def register(self):
        return import_yolo_splits(self.service.store,self.home/'app',self.root,self.splits,'fixture')

    def test_counts_pagination_and_original_label_access(self):
        d=self.register()
        self.assertEqual(d['sample_count'],10)
        self.assertFalse(d['content_verified'])
        self.assertEqual(d['splits'],{'train':7,'validation':2,'test':1})
        self.assertEqual(sample_page(d,'train',5,2)['items'][0]['index'],5)
        self.assertEqual(sample_page(d,'validation')['items'][0]['index'],7)
        self.assertEqual(sample_at(d,6)['image'],'images/train-6.jpg')
        base='/api/datasets/'+d['id']+'/samples'
        self.assertEqual(self.client.get(base+'?split=test').json()['items'][0]['index'],9)
        self.assertEqual(self.client.get(base+'/6/label').text,'0 0.5 0.5 0.2 0.2 0.5 0.5 2\n')
        self.assertEqual(self.client.get(base+'/10/image').status_code,404)
        self.assertEqual(self.client.get(base+'?offset=-1').status_code,422)
        self.assertEqual(self.register()['id'],d['id'])

    def test_duplicates_and_path_escape_rejected(self):
        self.splits['validation'].write_text(self.splits['train'].read_text())
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            self.register()
        self.splits['validation'].write_text('../outside.jpg\n')
        with self.assertRaises(ValueError):
            self.register()

    def test_indexed_content_cannot_be_used_for_live_training(self):
        d=self.register()
        template=self.service.get('template','ball-p2-v1')
        template.update(live_verified=True,image_digest='sha256:'+'a'*64)
        with self.service.store.connect() as db:
            self.service.store.put(db,'template',template)
        self.service.mode='live'
        with self.assertRaisesRegex(ValueError,'full content verification'):
            self.service.preview({'name':'live','template_id':template['id'],'dataset_ids':[d['id']],
                                  'overrides':{},'gpu':{'mode':'auto','count':1}})
