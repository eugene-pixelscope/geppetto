import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
import uvicorn
from playwright.sync_api import sync_playwright
from geppetto.app import create_app
from geppetto.workshop import reconcile
from PIL import Image

def load(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
model=REPO.parent/'sports-ml/models/jersey_recognition'
data=load('jersey_data_test',model/'data.py')
registration=load('jersey_registration',REPO/'tools/import_jersey_datasets.py')
with tempfile.TemporaryDirectory() as temp:
 home=Path(temp)/'app';root=Path(temp)/'data';sources=[]
 for i,(name,split) in enumerate([('first','train'),('second','train'),('validation','validation'),('test','test')]):
  folder=root/name/'7';folder.mkdir(parents=True)
  Image.new('RGB',(32,48),(30+i*50,20,30)).save(folder/'sample.png')
  sources.append(dict(name=name,path=name,format='class_folders',split=split))
 manifest=Path(temp)/'prepared.json';data.atomic_json(manifest,data.prepare(dict(root=str(root),sources=sources)))
 registered=registration.register(manifest,model,home)
 app=create_app(home,'catalog',False,auth_required=False);reconcile(app.state.service.store)
 server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
 thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
 try:
  for _ in range(100):
   if server.started:break
   time.sleep(.05)
  with sync_playwright() as p:
   browser=p.chromium.launch(headless=True)
   page=browser.new_page(viewport={'width':1440,'height':1100});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
   base=f'http://127.0.0.1:{port}/'
   page.goto(base+'#page=training-config&category=jersey-recognition');page.wait_for_selector('#training-form')
   page.locator('[name=project_id]').select_option('project-jersey-recognition')
   page.locator('[name=architecture_id]').select_option('jersey-resnet18')
   page.locator('[name=name]').fill('Jersey multi-source test');page.locator('#training-next').click()
   for source in registered[:3]:page.locator(f'[name=dataset_ids][value="{source["id"]}"]').check()
   assert '3 selected' in page.locator('#dataset-selection-summary').inner_text()
   page.screenshot(path=str(model/'reports/geppetto-selection.png'),full_page=True)
   page.locator('#training-next').click();page.locator('[name=sampling]').select_option('proportional');page.locator('#training-next').click()
   page.wait_for_selector('#training-export')
   with page.expect_download() as info:page.locator('#training-export').click()
   export=json.loads(Path(info.value.path()).read_text());assert len(export['manifests'])==3;assert export['parameters']['sampling']=='proportional'
   page.locator('#training-next').click();page.wait_for_function("() => document.querySelector('#training-saved-status').textContent.includes('Saved')")
   assert len(app.state.service.list('training_draft')['items'][0]['dataset_ids'])==3
   # Reopen saved selection and verify all three source choices survive.
   page.locator('[data-draft]').click();page.locator('#training-next').click()
   assert page.locator('[name=dataset_ids]:checked').count()==3
   assert page.get_by_role('heading',name='Training management').count()==1
   page.set_viewport_size({'width':375,'height':1000})
   assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
   widths=page.locator('.wizard-actions button').evaluate_all('(buttons)=>buttons.map(button=>Math.round(button.getBoundingClientRect().width))')
   assert abs(widths[0]-widths[1])<=2,widths
   page.set_viewport_size({'width':1440,'height':1100})
   page.goto(base+'#dataset='+registered[0]['id']);page.wait_for_selector('.label-summary')
   page.wait_for_function("() => document.querySelector('.label-summary').textContent.includes('7')")
   label=page.request.get(base+'api/datasets/'+registered[0]['id']+'/samples/0/label');assert label.json()['number']==7
   for width in (375,1440):
    page.set_viewport_size({'width':width,'height':1000})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
   with app.state.service.store.connect() as db:app.state.service.store.put(db,'execution_profile',{'id':'fixture-enabled-profile','enabled':True})
   page.goto(base+'#page=create&category=jersey-recognition');page.reload();page.wait_for_selector('#training-form')
   assert page.locator('[name=architecture_id]').input_value()=='jersey-resnet18'
   assert not errors,errors
   print('Browser: multiple selection, export, save/reopen, numeric label and mobile checks passed')
   browser.close()
 finally:
  server.should_exit=True;thread.join(10);sock.close()
