"""Exercise orchestration and resource UI against an isolated temporary catalog."""
from pathlib import Path
import socket
import sys
import threading
import time
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1]/'tests')]
import uvicorn
from playwright.sync_api import sync_playwright
from geppetto.app import create_app
from test_orchestration import OrchestrationTests
from test_platform import NODE


def main():
    fixture = OrchestrationTests(); fixture.setUp()
    server = None
    try:
        run = fixture.launch()
        metric = {'observed_at': time.time(), 'cpu_percent': 47, 'memory_used_bytes': 20*1024**3,
                  'memory_total_bytes': 64*1024**3, 'source': 'isolated-browser-fixture',
                  'gpus': [{'uuid': 'GPU-fixture', 'name': 'Fixture GPU', 'utilization_percent': 61,
                            'memory_used_mib': 1024, 'memory_total_mib': 81920, 'temperature_c': 51, 'power_watts': 180}]}
        fixture.post('/api/agent/snapshot', {**fixture.ident, 'sequence': 2, 'nodes': [{**NODE, 'telemetry': metric}],
            'pvcs': [{'uid': n+'-uid', 'name': n, 'namespace': 'training', 'phase': 'Bound'} for n in ('data', 'results')]}, fixture.auth)
        fixture.request['schedule'] = {'kind': 'cron', 'cron': '0 9 * * *', 'timezone': 'Asia/Seoul'}
        fixture.launch('schedule')
        app = create_app(fixture.home/'app', 'catalog', False, token='', auth_required=False)
        sock = socket.socket(); sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True); thread.start()
        for _ in range(100):
            if server.started: break
            time.sleep(.05)
        base = f'http://127.0.0.1:{port}/'
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(base+'#node='+fixture.node['id']+'&tab=resources')
            page.get_by_role('heading', name='Node resource usage').wait_for()
            page.get_by_text('Fixture GPU', exact=False).wait_for()
            assert page.locator('.resource-chart').count()==2
            assert '100%' in page.locator('.resource-chart').first.inner_text()
            assert '64.0 GiB' in page.locator('.resource-chart').nth(1).inner_text()
            heights=page.locator('.resource-usage-card').evaluate_all('(cards)=>cards.slice(0,2).map(card=>Math.round(card.getBoundingClientRect().height))')
            assert len(set(heights))==1,heights
            assert page.get_by_text('Managed jobs on this node',exact=False).count()==0
            page.goto(base+'#page=platform&tab=resources')
            page.locator('[name=max_gpus]').fill('2')
            page.get_by_role('button', name='Save allocation policy').click()
            page.get_by_text('Maximum GPUs: 2', exact=False).wait_for()
            page.goto(base+'#page=schedules')
            page.locator('#schedule-backfill [name=start]').fill('2026-01-01T00:00')
            page.locator('#schedule-backfill [name=end]').fill('2026-01-03T00:00')
            page.get_by_role('button', name='Review missing jobs').click()
            page.locator('#backfill-start').wait_for(); page.locator('#backfill-start').click()
            page.get_by_text(' jobs added to the queue.', exact=False).wait_for()
            page.goto(base+'#page=platform&tab=pipelines')
            page.locator('#simple-pipeline [name=base]').select_option(run['id'])
            page.locator('#simple-pipeline [name=split]').select_option('validation')
            page.get_by_role('button', name='Review pipeline', exact=True).click()
            page.locator('#pipeline-start').wait_for(); page.locator('#pipeline-start').click()
            page.locator('[data-stop-pipeline]').wait_for()
            fixture.poll(); fixture.finish(run, 'failed')
            page.goto(base+'#run='+run['id'])
            page.locator('.run-tabs').get_by_role('link',name='Execution').click()
            page.locator('#retry-run').wait_for(); page.locator('#retry-run').click()
            page.wait_for_function('(old) => new URLSearchParams(location.hash.slice(1)).get("run") !== old', arg=run['id'])
            for width in (320, 375, 768, 1440):
                page.set_viewport_size({'width': width, 'height': 900})
                for route, selector in [('#node='+fixture.node['id']+'&tab=resources', '#resource-refresh'),
                                        ('#page=platform&tab=resources', '[name=max_gpus]'),
                                        ('#page=platform&tab=pipelines', '#simple-pipeline'),
                                        ('#page=schedules', '#schedule-backfill')]:
                    page.goto(base+route)
                    try: page.wait_for_selector(selector,timeout=8000)
                    except Exception:
                        print('ROUTE FAILURE',width,route,'url',page.url,'main',page.locator('#main').inner_text()[:600],'toast',page.locator('#toast').inner_text(),'errors',errors,flush=True)
                        raise
                    page.wait_for_timeout(100)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, route)
            page.screenshot(path=str(Path(__file__).resolve().parents[1]/'local/orchestration-preview.png'), full_page=True)
            assert not errors, errors
            browser.close()
            print('PASS: resources, GPU policies, backfill preview/submit, linked pipeline, failed-stage retry; four widths; no JS errors')
    finally:
        if server:
            server.should_exit = True; thread.join(timeout=5); sock.close()
        fixture.tearDown()


if __name__ == '__main__': main()
