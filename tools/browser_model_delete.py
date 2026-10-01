"""Model card actions, detail visibility, permanent deletion on an isolated catalog."""
from pathlib import Path
import socket,sys,tempfile,threading,time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import uvicorn
from playwright.sync_api import sync_playwright
from geppetto.app import create_app
from geppetto.workshop import topology


def main():
    with tempfile.TemporaryDirectory() as directory:
        app=create_app(directory,'demo',False,auth_required=False);service=app.state.service
        topology(service)
        with service.store.connect() as db:
            runs=service.store.all(db,'run')
            for run in runs:
                run['state']='succeeded';service.store.put(db,'run',run)
            for s in service.store.all(db,'schedule'):
                s['enabled']=False;service.store.put(db,'schedule',s)
        run=runs[0];pid=run['pinocchio_id'];node=run['node_id']
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,log_level='error'));thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        try:
            for _ in range(100):
                if server.started:break
                time.sleep(.05)
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1000})
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)));base=f'http://127.0.0.1:{port}/'
                page.goto(base+'#node='+node);page.wait_for_selector('[data-model-delete="'+pid+'"]')
                for width in (320,375,768,1440):
                    page.set_viewport_size({'width':width,'height':1000})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
                page.screenshot(path='local/model-delete-cards.png',full_page=True)
                original_url=page.url
                edit=page.locator('[data-action="rename"][data-id="'+pid+'"]')
                delete=page.locator('[data-model-delete="'+pid+'"]')
                assert delete.locator('svg').count()==1
                assert edit.bounding_box()['y']<delete.bounding_box()['y']
                edit.click()
                assert page.url==original_url
                page.locator('#modal input[name="name"]').fill('편집한 모델')
                page.locator('#modal-form button:not([type="button"])').click()
                page.locator('.model-entry h3',has_text='편집한 모델').wait_for()
                page.locator('[data-model-delete="'+pid+'"]').click()
                assert page.url==original_url
                page.locator('#model-delete-cancel').click()
                page.locator('[data-model-delete="'+pid+'"]').click();page.locator('#model-delete-confirm').click()
                page.locator('[data-model-delete="'+pid+'"]').wait_for(state='detached')
                assert run['id'] not in [r['id'] for r in page.request.get(base+'api/catalog/run').json()['items']]
                assert page.get_by_role('link',name='삭제된 모델',exact=True).count()==0
                assert page.request.get(base+'api/catalog/run/'+run['id']).status==404
                assert page.request.get(base+'api/catalog/pinocchio/'+pid).status==404
                other=next(r for r in runs if r['pinocchio_id']!=pid)
                page.goto(base+'#run='+other['id']);page.wait_for_selector('.run-execution-info')
                assert page.locator('[data-model-delete],[data-kind="pinocchio"]').count()==0
                page.goto(base+'#page=jobs');page.wait_for_selector('.run-table')
                assert page.locator('[data-history-delete],[data-history-bulk="delete"]').count()==0
                with service.store.connect() as db:
                    r=service.store.get(db,'run',other['id']);r['state']='running';service.store.put(db,'run',r)
                page.goto(base+'#node='+node);page.reload();page.wait_for_selector('[data-model-delete="'+other['pinocchio_id']+'"]')
                assert page.locator('[data-model-delete="'+other['pinocchio_id']+'"]').is_disabled()
                assert not errors,errors
                browser.close();print('PASS: model card edit/delete, hidden detail actions, cancel, permanent deletion, active guard, no Runs delete controls, responsive cards.')
        finally:server.should_exit=True;thread.join(timeout=10)
if __name__=='__main__':main()
