"""Browser verification of diagnostics and all management tabs on a test catalog."""
from pathlib import Path
import socket,sys,threading,time
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'tests')]
import uvicorn
from playwright.sync_api import sync_playwright
from test_platform import PlatformTests
from geppetto.visualizations import publish
fixture=PlatformTests();fixture.setUp();run=fixture.launch('diagnostics-browser')
for kind,data in [('histogram',{'edges':[0,1,2],'counts':[2,8]}),('embedding',{'projection':'pca','points':[{'x':0,'y':0,'label':'zero'},{'x':1,'y':1,'label':'one'}]}),('trace',{'events':[{'name':'forward','lane':'GPU','start_us':0,'duration_us':200}]})]:
    publish(fixture.service,run['id'],{'type':kind,'name':kind,'step':0,'data':data})
sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
server=uvicorn.Server(uvicorn.Config(fixture.app,log_level='error'));thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
try:
    while not server.started:time.sleep(.05)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page();errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)));base=f'http://127.0.0.1:{port}'
        assert page.request.post(base+'/api/auth/setup',data={'username':'admin','password':'browser-fixture-password','setup_key':fixture.app.state.accounts.setup_file.read_text()},headers={'X-Geppetto-Request':'1'}).ok
        for width in [375,1440]:
            page.set_viewport_size({'width':width,'height':1000});page.goto(base+'/#run='+run['id']);page.wait_for_selector('#diagnostic-charts svg')
            assert page.locator('#diagnostic-charts svg').count()==3
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            for tab in ('pipelines','registry','sweeps'):
                page.goto(base+'/#page=platform&tab='+tab);page.wait_for_selector('#platform-content form')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        assert not errors,errors
        print('PASS: histogram, embedding, trace; pipeline, registry, tuning forms; desktop/mobile; no browser errors')
        browser.close()
finally:
    server.should_exit=True;thread.join(timeout=5);sock.close();fixture.tearDown()
