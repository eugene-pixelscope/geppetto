"""Delete and restore isolated demo history in a real browser; no external Jobs."""
from pathlib import Path
import socket,sys,tempfile,threading,time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import uvicorn
from playwright.sync_api import sync_playwright
from geppetto.app import create_app


def main():
    with tempfile.TemporaryDirectory() as directory:
        app=create_app(directory,'demo',False,auth_required=False)
        service=app.state.service
        # Stable test states, with workers disabled.
        with service.store.connect() as db:
            runs=service.store.all(db,'run')
            for i,run in enumerate(runs):
                run['state']='running' if i==0 else 'succeeded'
                service.store.put(db,'run',run)
        selected=runs[1]
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        try:
            for _ in range(100):
                if server.started:break
                time.sleep(.05)
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1000})
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)));base=f'http://127.0.0.1:{port}/'
                headers={'X-Geppetto-Request':'1'}
                for run in runs[1:3]:
                    assert page.request.post(base+'api/run-history/delete',data={'ids':[run['id']],'confirmation':'delete'},headers=headers).ok
                page.goto(base+'#page=trash');page.wait_for_selector('[data-history-restore]')
                assert page.locator('[data-history-restore]').count()==2
                page.locator('[data-history-all]').check()
                page.locator('[data-history-bulk="restore"]').click();page.locator('#history-confirm').click()
                page.get_by_text('No deleted run history.',exact=True).wait_for()
                for width in (320,375,768,1440):
                    page.set_viewport_size({'width':width,'height':900})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
                assert not errors,errors
                assert page.request.get(base+'api/catalog/run?visibility=deleted').json()['total']==0
                browser.close();print('PASS: legacy individual history bulk restoration and responsive trash view.')
        finally:
            server.should_exit=True;thread.join(timeout=10)


if __name__=='__main__':main()
