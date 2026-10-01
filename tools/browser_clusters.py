"""Use only an isolated catalog server on localhost:8092. Never the user's catalog."""
from pathlib import Path
import sys
import tempfile
import httpx
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from geppetto.agent import identity,cycle
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
BASE='http://127.0.0.1:8092'
class FixtureKube:
    def cluster_uid(self):return 'isolated-cluster-uid'
    def snapshot(self):
        return {'nodes':[{'uid':'isolated-node-uid','name':'gpu-fixture','ready':'True','unschedulable':False,
                         'capacity':{'cpu':'32','memory':'64Gi','nvidia.com/gpu':'2'},
                         'allocatable':{'cpu':'30','memory':'60Gi','nvidia.com/gpu':'2'},'kubelet_version':'test'}],'pvcs':[]}
with tempfile.TemporaryDirectory(dir=ROOT/'local') as directory,sync_playwright() as p,httpx.Client(base_url=BASE,timeout=10) as central:
    assert not central.get('/api/clusters').json()['items'],'Use a fresh isolated server'
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1000})
    errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(BASE+'/#page=clusters')
    page.locator('#cluster-new').click()
    page.locator('#cluster-form input').fill('격리된 Agent 검증')
    with page.expect_response(lambda r:r.url==BASE+'/api/clusters' and r.request.method=='POST') as result:
        page.get_by_role('button',name='Create registration token').click()
    c=result.value.json()
    page.wait_for_selector('#download-agent')
    page.locator('#central-url').fill('https://central.example.test')
    with page.expect_download() as download:
        page.locator('#download-agent').click()
    assert download.value.suggested_filename=='geppetto-agent.yaml'
    page.locator('#close-cluster').click()
    path=Path(directory)/'identity.json';state=identity(path,c['id'],c['bootstrap_token'])
    try:cycle(central,FixtureKube(),state,path,c['bootstrap_token'])
    except httpx.HTTPStatusError as e:assert e.response.status_code==403
    page.locator('#refresh').click()
    page.get_by_role('button',name='Approve agent',exact=True).click()
    page.get_by_text('Awaiting node discovery',exact=True).wait_for()
    cycle(central,FixtureKube(),state,path,c['bootstrap_token'])
    page.locator('#refresh').click()
    page.get_by_text('Agent connected',exact=True).wait_for()
    for width in [320,375,768,1440]:
        page.set_viewport_size({'width':width,'height':1000})
        page.wait_for_timeout(150)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
    page.get_by_role('button',name='Register node',exact=True).click()
    page.get_by_role('heading',name='gpu-fixture',level=1,exact=True).wait_for()
    assert 'Read-only' in page.locator('main').inner_text()
    page.goto(BASE+'/#page=whales')
    page.wait_for_selector('.environment-row')
    assert page.locator('.environment-row').count()==2
    page.screenshot(path=str(ROOT/'local/agent-validation-home.png'),full_page=True)
    assert not errors,errors
    print('PASS isolated server: create -> manifest download -> enroll -> approve -> inventory -> adopt; 4 widths')
    browser.close()
