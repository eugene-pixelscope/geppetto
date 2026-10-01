"""Exercise management screens against a temporary, isolated in-process server."""
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import uvicorn
from playwright.sync_api import sync_playwright
from functools import partial
from geppetto.app import create_app as _create_app
# These isolated fixtures exercise catalog behavior without browser authentication.
create_app = partial(_create_app, auth_required=False)


def main():
    with tempfile.TemporaryDirectory(prefix='geppetto-platform-') as directory:
        app=create_app(Path(directory),'demo',False)
        service=app.state.service
        server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        try:
            for _ in range(100):
                if server.started:break
                time.sleep(.05)
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True)
                page=browser.new_page(viewport={'width':1440,'height':1000})
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                base=f'http://127.0.0.1:{port}/'
                page.goto(base+'#page=platform&tab=metrics');page.wait_for_selector('#metric-form')
                page.locator('[name=key]').fill('accuracy');page.locator('[name=label]').fill('Accuracy')
                page.locator('#metric-form button').click();page.get_by_text('Accuracy',exact=True).wait_for()
                assert len(service.list('metric_definition')['items'])==1
                run=next(r for r in service.list('run')['items'] if r['category']=='ball-detection')
                body={'run_id':run['id'],'dataset_id':'demo-ball-detection','type':'classification',
                      'policy':{'evaluator_version':'browser-fixture','checkpoint_sha256':'a'*64,'split':'train'},
                      'samples':[{'index':0,'target':'12','prediction':'21','confidence':.8}]}
                page.goto(base+'#page=platform&tab=evaluation');page.wait_for_selector('#evaluation-file')
                page.locator('#evaluation-file').set_input_files({'name':'evaluation.json','mimeType':'application/json','buffer':json.dumps(body).encode()})
                page.locator('#evaluation-report option').nth(1).wait_for(state='attached')
                page.locator('#evaluation-report').select_option(index=1)
                page.get_by_role('heading',name='Confusion matrix · Ground truth → Prediction counts').wait_for()
                assert page.locator('#report-body img').count()==1
                page.goto(base+'#page=platform&tab=sweeps');page.wait_for_selector('#sweep-form')
                page.locator('[name=base]').select_option(run['id']);page.locator('[name=metric]').fill('accuracy')
                page.locator('[name=parameters]').fill('{"batch":[4,8]}')
                page.locator('#sweep-form button').first.click();page.wait_for_selector('#sweep-start')
                page.locator('#sweep-start').click();page.wait_for_selector('[data-sweep]')
                assert service.list('sweep')['total']==1
                page.locator('[data-sweep]').click();page.get_by_text('Best completed result: Not yet available').wait_for()
                page.goto(base+'#run='+run['id']);page.wait_for_selector('#run-annotations')
                page.locator('#run-annotations [name=tags]').fill('baseline, reviewed')
                page.locator('#run-annotations [name=note]').fill('Browser test note')
                page.locator('#run-annotations button').click();page.get_by_text('Saved.',exact=True).wait_for()
                assert service.get('run',run['id'])['tags']==['baseline','reviewed']
                page.goto(base+'#page=projects&project=project-ball-detection')
                page.wait_for_selector('#project-values table')
                for width in (320,375,768,1440):
                    page.set_viewport_size({'width':width,'height':900})
                    for tab in ('execution','data','evaluation','sweeps','metrics'):
                        page.goto(base+'#page=platform&tab='+tab)
                        page.wait_for_selector('#platform-content .card')
                        page.wait_for_timeout(100)
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,tab)
                assert not errors,errors
                browser.close()
                print('PASS: metric registration, typed evaluation import/view, sweep preview/start/results; 5 tabs × 4 widths; no JS errors')
        finally:
            server.should_exit=True;thread.join(timeout=5);sock.close()


if __name__=='__main__':main()
