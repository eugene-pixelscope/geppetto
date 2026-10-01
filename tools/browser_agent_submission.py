"""Verify the Agent submission UI on a temporary catalog; no Agent is running."""
from pathlib import Path
import socket
import sys
import threading
import time
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'tests')]
import uvicorn
from playwright.sync_api import sync_playwright
from test_platform import PlatformTests

fixture=PlatformTests();fixture.setUp()
sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
server=uvicorn.Server(uvicorn.Config(fixture.app,log_level='error'))
thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
try:
    for _ in range(100):
        if server.started:break
        time.sleep(.05)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        base=f'http://127.0.0.1:{port}'
        response=page.request.post(base+'/api/auth/setup',data={'username':'admin','password':'browser-fixture-password','setup_key':fixture.app.state.accounts.setup_file.read_text()},headers={'X-Geppetto-Request':'1'})
        assert response.ok
        page.goto(base+'/#page=create&node='+fixture.node['id'])
        page.locator('#run-name').fill('Browser Agent submission')
        page.locator('.dataset-check').check()
        page.locator('#wizard-next').click();page.wait_for_selector('#wizard-model')
        page.locator('#wizard-next').click();page.wait_for_selector('#schedule-kind')
        page.locator('#wizard-next').click();page.get_by_role('heading',name='Browser Agent submission').wait_for()
        assert fixture.service.list('run')['total']==0
        page.locator('#wizard-next').click();page.wait_for_selector('#run-annotations')
        runs=fixture.service.list('run')['items'];assert len(runs)==1
        assert runs[0]['node_id']==fixture.node['id'] and runs[0]['state']=='queued'
        assert runs[0]['frozen']['execution']['backend']=='agent' and not runs[0].get('dispatch')
        with fixture.service.store.connect() as db:
            source=runs[0];source.update(state='succeeded',artifacts=[{'id':'best','path':'completed/best.pt','complete':True,
                'role':'inference_weights','sha256':'a'*64,'artifact_uri':'mlflow-artifacts:/1/run/artifacts','size':1}])
            fixture.service.store.put(db,'run',source)
        page.reload();page.wait_for_selector('#evaluation-launcher form')
        page.locator('#evaluation-launcher [name=split]').select_option('validation')
        page.get_by_role('button',name='Review evaluation settings',exact=True).click()
        page.wait_for_selector('#evaluation-start');page.locator('#evaluation-start').click()
        page.wait_for_url('**/#run=*')
        page.wait_for_timeout(300)
        evaluation=next(r for r in fixture.service.list('run')['items'] if r['id']!=source['id'])
        assert evaluation['frozen']['relation']=='evaluate' and evaluation['pinocchio_id']==source['pinocchio_id']
        assert not errors,errors
        print('PASS: catalog mode → remote node/shared dataset → confirmed training + checkpoint evaluation; durable Agent runs; no Job submitted')
        browser.close()
finally:
    server.should_exit=True;thread.join(timeout=5);sock.close();fixture.tearDown()
