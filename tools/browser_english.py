"""Check English UI text and responsive layout against an isolated demo catalog."""
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
import time

import uvicorn
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from geppetto.app import create_app


def main():
    for path in (ROOT / 'web').iterdir():
        if path.suffix in ('.js', '.html', '.css'):
            assert not re.search('[가-힣]', path.read_text()), path
    with tempfile.TemporaryDirectory(prefix='geppetto-english-') as directory:
        app = create_app(directory, 'demo', False, token='english-browser-test')
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        base = f'http://127.0.0.1:{sock.getsockname()[1]}/'
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(.05)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                # English must remain the UI language even in a Korean browser.
                page = browser.new_page(locale='ko-KR', viewport={'width': 1440, 'height': 1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base + '#setup=' + (Path(directory) / 'initial-admin-key').read_text())
                page.locator('#new-password').fill('english-browser-password')
                page.locator('#password-confirm').fill('english-browser-password')
                page.get_by_role('button', name='Create admin account', exact=True).click()
                page.locator('#auth-screen').wait_for(state='detached')
                page.locator('#main h1').wait_for()
                assert page.locator('html').get_attribute('lang') == 'en'
                runs = page.request.get(base + 'api/catalog/run').json()['items']
                routes = ['page=' + name for name in (
                    'home', 'projects', 'clusters', 'jobs', 'datasets', 'models',
                    'schedules', 'settings', 'users', 'account', 'trash', 'create')]
                routes += ['page=platform&tab=' + tab for tab in (
                    'execution', 'resources', 'data', 'evaluation', 'sweeps', 'metrics', 'registry', 'pipelines')]
                routes += ['node=whale-local', 'node=whale-local&tab=resources',
                           'dataset=demo-ball-detection', 'run=' + runs[0]['id']]
                for width in (375, 1440):
                    page.set_viewport_size({'width': width, 'height': 1000})
                    for route in routes:
                        page.goto(base + '#' + route)
                        page.wait_for_function('document.querySelector("#main h1") !== null')
                        page.wait_for_timeout(300)
                        text = page.locator('body').inner_text()
                        assert not re.search('[가-힣]', text), (width, route, text)
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, route)
                assert not errors, errors
                browser.close()
                print(f'English UI passed: {len(routes)} routes at desktop and mobile widths, Korean browser locale, no JS errors.')
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == '__main__':
    main()
