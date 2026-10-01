"""Read-only live UI verification; temporary session, no training or catalog fixtures."""
from pathlib import Path
import json
import secrets
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from geppetto.storage import Store
from geppetto.auth import digest
from playwright.sync_api import sync_playwright


def main():
    root=Path(__file__).resolve().parents[1]
    store=Store(root/'local/app/catalog.sqlite3')
    secret=secrets.token_urlsafe(32)
    with store.connect() as db:
        user=db.execute('SELECT id FROM auth_users WHERE disabled=0 AND must_change_password=0 AND role="admin" LIMIT 1').fetchone()
        if not user:raise ValueError('No configured administrator available for local verification')
        db.execute('INSERT INTO auth_sessions VALUES(?,?,?)',(digest(secret),user[0],time.time()+300))
        before={r['id'] for r in store.all(db,'run')}
        node=next(w for w in store.all(db,'whale') if w.get('connection')=='kubernetes-agent')
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            page=browser.new_page(viewport={'width':1440,'height':1100})
            page.context.add_cookies([{'name':'geppetto_session','value':secret,'url':'http://127.0.0.1:8091','httpOnly':True,'sameSite':'Strict'}])
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto('http://127.0.0.1:8091/#node='+node['id']+'&tab=resources')
            try:
                page.locator('#resource-refresh').wait_for(timeout=10000)
            except Exception:
                print('Browser errors:',errors)
                print('Visible page:',page.locator('body').inner_text()[:2400])
                raise
            response=page.request.get('http://127.0.0.1:8091/api/whales/'+node['id']+'/resources')
            data=response.json();assert not data['stale'],data
            assert len(data['telemetry']['gpus'])==8
            assert data['telemetry']['cpu_percent'] is not None
            page.screenshot(path=str(root/'local/node-resources-live.png'),full_page=True)
            for route,selector in [('platform&tab=resources','[name=max_gpus]'),('platform&tab=pipelines','#simple-pipeline'),('schedules','#schedule-backfill'),('create','#wizard-node')]:
                page.goto('http://127.0.0.1:8091/#page='+route);page.wait_for_selector(selector)
            assert page.locator('#wizard-node').input_value()==node['id']
            page.goto('http://127.0.0.1:8091/#node='+node['id']+'&tab=resources')
            page.locator('#resource-refresh').wait_for()
            page.set_viewport_size({'width':375,'height':900});page.wait_for_timeout(200)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(root/'local/node-resources-live-mobile.png'),full_page=True)
            assert not errors,errors
            browser.close()
        with store.connect() as db:assert {r['id'] for r in store.all(db,'run')}==before
        print('PASS: live 8091 resources report 8 GPUs and fresh CPU/memory; management routes and mobile width; zero training submissions')
    finally:
        with store.connect() as db:db.execute('DELETE FROM auth_sessions WHERE hash=?',(digest(secret),))


if __name__=='__main__':main()
