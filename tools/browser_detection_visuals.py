"""Isolated browser checks for epoch axes, density heatmaps, and RGB box overlays."""
from pathlib import Path
import socket,sys,tempfile,threading,time,json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import uvicorn
from playwright.sync_api import sync_playwright
from geppetto.app import create_app


def main():
    with tempfile.TemporaryDirectory() as directory:
        app=create_app(directory,'demo',False,auth_required=False);service=app.state.service
        run=next(r for r in service.list('run')['items'] if r['category']=='ball-detection')
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,log_level='error'));thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        try:
            for _ in range(100):
                if server.started:break
                time.sleep(.05)
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1000})
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)));base=f'http://127.0.0.1:{port}/'
                page.add_init_script("localStorage.setItem('geppetto-scalars:"+run['id']+"',JSON.stringify({axis:'elapsed'}));localStorage.setItem('geppetto-chart-v2:scalar-chart-0',JSON.stringify({'axis-select':'step'}));")
                page.goto(base+'#run='+run['id']);page.wait_for_selector('.chart-x-label')
                assert page.locator('#scalar-axis,.axis-select').count()==0
                assert set(page.locator('.chart-x-label').all_text_contents())=={'Epoch'}
                page.goto(base+'#run='+run['id']+'&view=errors')
                page.get_by_text('＋ Import evaluation predictions',exact=True).click();page.locator('#evaluation-demo').click()
                page.wait_for_selector('#detection-heatmap canvas')
                page.screenshot(path='/tmp/geppetto-error-analysis.png',full_page=True)
                assert page.locator('.heat-cell').count()==0
                page.wait_for_selector('.label-overlay [data-overlay=truth] rect[stroke="#22c55e"]')
                assert page.locator('.label-overlay [data-overlay=truth] rect[stroke="#ef4444"]').count()>0
                assert page.locator('#detection-heatmap canvas').evaluate('(c)=>{const p=c.getContext("2d").getImageData(0,0,c.width,c.height).data;return p.some((v,i)=>i%4===3&&v>0)}')
                page.locator('[data-open-failure]').first.click();page.wait_for_selector('#failure-canvas')
                assert page.locator('#failure-canvas [data-overlay=truth] rect[stroke="#22c55e"]').count()>0
                page.locator('#failure-fit').click();page.screenshot(path='local/detection-overlay-fixed.png')
                page.locator('#failure-truth').uncheck()
                assert page.locator('#failure-canvas [data-overlay=truth]').evaluate('(g)=>g.style.display')=='none'
                page.locator('#failure-truth').check();page.locator('#failure-close').click()
                page.locator('#analysis-kind').select_option('fp')
                page.wait_for_selector('.label-overlay [data-overlay=prediction] rect[stroke="#fb923c"]')
                page.locator('#analysis-metric').select_option('rate')
                page.wait_for_function('()=>document.querySelector(".heatmap-summary")?.textContent.includes("local error rate")')
                page.locator('#detection-heatmap canvas').click(position={'x':40,'y':40})
                page.wait_for_selector('.heatmap-selection:not([hidden])')
                page.locator('#analysis-clear').click();page.wait_for_selector('.heatmap-selection[hidden]',state='attached')
                for width in (320,375,768,1440):
                    page.set_viewport_size({'width':width,'height':1000})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
                page.screenshot(path='local/detection-density-heatmap.png',full_page=True)
                page.set_viewport_size({'width':375,'height':1000})
                page.evaluate('''async()=>{const host=document.createElement("div");host.id="gradcam-mobile-fixture";document.querySelector("main").replaceChildren(host);const {mountGradcam}=await import("/static/gradcam.js");const items=Array.from({length:6},(_,i)=>({index:i,image:"sample-"+i+".jpg"}));const api=async path=>path.includes("/samples?")?{items:items.slice(0,2),total:6}:path.endsWith("/gradcam")?{checkpoints:[{id:"weight",path:"best.pt"}],profiles:[{id:"node",node_name:"GPU node",template_id:"yolo"}],datasets:[{id:"dataset",name:"Ball",version:"123456789"}],runs:[]}:{};await mountGradcam(host,{id:"run"},api);}''')
                page.wait_for_selector('.gradcam-samples .cam-sample')
                assert page.locator('.gradcam-samples .cam-sample').count()==2
                assert page.locator('.gradcam-samples input[type=radio]').count()==0
                sample=page.locator('.gradcam-samples .cam-sample').first;sample.click();assert sample.get_attribute('aria-pressed')=='true'
                assert page.locator('[data-cam=preview]').evaluate('(button)=>button.getBoundingClientRect().width/button.parentElement.getBoundingClientRect().width>.9')
                assert not errors,errors
                browser.close();print('PASS: epoch fixed despite saved axes; continuous density/rate/cell drilldown; green GT plus red FN and orange FP; thumbnails/viewer; responsive.')
        finally:server.should_exit=True;thread.join(timeout=10)
if __name__=='__main__':main()
