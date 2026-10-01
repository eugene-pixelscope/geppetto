"""Exercise project navigation and shared panels against an isolated demo catalog."""
from pathlib import Path
import socket, sys, tempfile, threading, time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from playwright.sync_api import sync_playwright
from geppetto.app import create_app

with tempfile.TemporaryDirectory() as directory:
    app = create_app(directory, 'demo', False, auth_required=False)
    with app.state.service.store.connect() as db:
        runs = app.state.service.store.all(db, 'run')
        project_id = runs[0]['project_id']
        for run in runs[:3]:
            run['project_id'] = project_id
            run['metrics'] = {f'train/loss_{i}': [{'epoch': j, 'step': j, 'value': (i+1)/(j+1)} for j in range(12)] for i in range(9)}
            app.state.service.store.put(db, 'run', run)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    try:
        for _ in range(100):
            if server.started: break
            time.sleep(.05)
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            page=browser.new_page(viewport={'width':1600,'height':1000})
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            base=f'http://127.0.0.1:{port}/'
            page.goto(base)
            page.wait_for_selector('#project-table tr')
            page.locator('#project-list-search').fill('zzzz-no-project')
            assert 'No matching projects' in page.locator('#project-table').inner_text()
            page.locator('#project-list-search').fill('')
            page.screenshot(path='/tmp/geppetto-projects.png',full_page=True)
            page.goto(base+'#page=jobs');page.wait_for_selector('#table-select-all')
            assert 'HYPERPARAMETERS' in page.locator('#table-head').inner_text()
            page.locator('#table-select-all').check()
            assert page.locator('.table-select:checked').count()==page.locator('.table-select').count()
            page.locator('#table-select-all').uncheck()
            assert page.locator('.table-select:checked').count()==0
            page.goto(base+f'#page=projects&project={project_id}')
            page.wait_for_selector('.scalar-card')
            assert page.locator('.scalar-card').count()==6
            assert page.locator('.comparison-values').count()==0
            collapse=page.locator('#panel-collapse');assert collapse.get_attribute('aria-expanded')=='true'
            collapse.click();assert collapse.get_attribute('aria-expanded')=='false'
            collapse.click();assert collapse.get_attribute('aria-expanded')=='true'
            page.locator('#panel-next').click()
            assert page.locator('.scalar-card').count()==3
            page.locator('#refresh').click();page.wait_for_timeout(300)
            assert page.locator('.scalar-card').count()==3
            page.locator('#scalar-search').fill('loss_[01]$')
            page.locator('#panel-regex').click()
            assert page.locator('.scalar-card').count()==2
            page.locator('#scalar-search').fill('[')
            assert page.locator('#panel-error').is_visible()
            page.locator('#scalar-search').fill('')
            page.locator('#panel-regex').click()
            page.locator('.panel-menu summary').first.click()
            page.locator('.panel-remove').first.click()
            assert page.locator('#scalar-count').inner_text()=='8'
            page.locator('#add-panels').click()
            page.locator('.panel-picker input').first.check()
            page.locator('#panel-apply').click()
            assert page.locator('#scalar-count').inner_text()=='9'
            first=page.locator('.project-visibility').first
            first.click();page.wait_for_timeout(200)
            assert page.locator('.chart-smoothed').count()==6*page.locator('.project-visibility[aria-pressed=true]').count(), (page.locator('.chart-smoothed').count(),page.locator('.project-visibility[aria-pressed=true]').count())
            first.click();page.wait_for_timeout(200)
            page.locator('.panel-settings summary').click()
            page.locator('#scalar-smoothing-value').fill('0.5')
            assert page.locator('.chart-raw').count()>0
            page.locator('.panel-settings summary').click()
            order=page.locator('.scalar-card').evaluate_all('(cards)=>cards.map(card=>card.dataset.metric)')
            page.locator('.panel-drag').first.drag_to(page.locator('.scalar-card').nth(2))
            moved=page.locator('.scalar-card').evaluate_all('(cards)=>cards.map(card=>card.dataset.metric)')
            assert moved[:3]==[order[1],order[0],order[2]],(order,moved)
            handle=page.locator('.panel-resize').first
            box=handle.bounding_box()
            page.mouse.move(box['x']+box['width']/2,box['y']+box['height']/2)
            page.mouse.down();page.mouse.move(box['x']+box['width']/2+410,box['y']+box['height']/2+65,steps=8);page.mouse.up()
            size=page.locator('.scalar-card').first.get_attribute('style')
            assert '--panel-cols: 2' in size and '--panel-height: 345px' in size,size
            page.reload();page.wait_for_selector('.scalar-card')
            assert page.locator('.scalar-card').first.get_attribute('data-metric')==moved[0]
            assert '--panel-cols:2' in page.locator('.scalar-card').first.get_attribute('style')
            page.locator('.project-tabs').get_by_role('link',name='Table').click()
            page.wait_for_selector('.runs-table tbody tr')
            assert page.locator('.runs-table tbody tr').count()>=2
            eye=page.locator('.table-visibility').first
            previous=eye.get_attribute('aria-pressed');eye.click()
            assert eye.get_attribute('aria-pressed')!=previous
            eye.click()
            assert eye.get_attribute('aria-pressed')==previous
            page.locator('.runs-table-search input').fill('no-such-run')
            assert 'No matching runs' in page.locator('.runs-table').inner_text()
            page.locator('.runs-table-search input').fill('')
            page.locator('.column-menu summary').click()
            page.locator('.column-menu input[value="runtime"]').uncheck()
            assert 'RUNTIME' not in page.locator('#table-head').inner_text()
            for width in (320,375,768,1440):
                page.set_viewport_size({'width':width,'height':900})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),('table',width)
            page.set_viewport_size({'width':1600,'height':1000})
            page.screenshot(path='/tmp/geppetto-runs-table.png',full_page=True)
            page.locator('.project-tabs').get_by_role('link',name='Charts').click()
            page.wait_for_selector('.scalar-card')
            page.screenshot(path='/tmp/geppetto-workspace.png',full_page=True)
            for width in (320,375,768,1440):
                page.set_viewport_size({'width':width,'height':1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),('project',width)
            page.set_viewport_size({'width':1600,'height':1000})
            page.locator('#scalar-search').fill('loss_')
            page.wait_for_timeout(16500)
            assert page.locator('#scalar-search').input_value()=='loss_'
            page.locator('#scalar-search').fill('')
            page.locator('.project-run a').first.click()
            page.wait_for_selector('#run-scalars .scalar-card')
            assert not page.locator('.workshop-scene').count()
            assert page.locator('.solo-chart .legend').first.is_hidden()
            page.locator('.solo-chart .chart-hitarea').first.hover(position={'x':60,'y':50})
            assert page.locator('.tooltip-single').first.is_visible()
            page.screenshot(path='/tmp/geppetto-run.png',full_page=True)
            for label,selector in [('Overview','.run-overview'),('Logs','.log'),('Files','#run-content table'),('Execution','#run-recovery'),('Charts','#run-scalars')]:
                page.locator('.run-tabs').get_by_role('link',name=label,exact=True).click()
                page.wait_for_selector(selector)
            for width in (320,375,768,1440):
                page.set_viewport_size({'width':width,'height':1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),('run',width)
            page.get_by_role('link',name='Back to project',exact=True).click()
            page.wait_for_selector('#project-run-list')
            page.goto(base+'#page=jobs');page.wait_for_selector('.runs-table tbody tr')
            assert page.locator('.runs-table tbody tr').count()>=3
            assert page.locator('#table-select-all').evaluate('(el)=>el.getBoundingClientRect().width')>=18
            assert not errors,errors
            browser.close()
            print('PASS: project search, 9 metric panels, pagination, regex validation, add/remove, run selection, smoothing, run tabs, back navigation, responsive widths, no JS errors')
    finally:
        server.should_exit=True;thread.join(timeout=10)
