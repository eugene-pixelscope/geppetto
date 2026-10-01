"""Exercise actual account UI and API boundaries against an isolated server."""
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from playwright.sync_api import sync_playwright
from geppetto.app import create_app

PASSWORD = 'browser-test-password-123'


def main():
    with tempfile.TemporaryDirectory(prefix='geppetto-accounts-') as directory:
        app = create_app(directory, 'demo', False, token='browser-test-machine-token')
        setup_key = (Path(directory)/'initial-admin-key').read_text()
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        sock = socket.socket();sock.bind(('127.0.0.1', 0));port = sock.getsockname()[1]
        thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:break
                time.sleep(.05)
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width':1440,'height':1000})
                errors = [];page.on('pageerror', lambda e:errors.append(str(e)))
                base = f'http://127.0.0.1:{port}/'
                page.goto(base+'#setup='+setup_key)
                page.get_by_role('heading',name='Create admin account').wait_for()
                assert 'setup=' not in page.url
                for width in (375,1440):
                    page.set_viewport_size({'width':width,'height':1000})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path='local/accounts-setup.png',full_page=True)
                page.locator('#new-password').fill(PASSWORD)
                page.locator('#password-confirm').fill('nonmatching-test-password')
                page.get_by_role('button',name='Create admin account').click()
                page.get_by_text('Passwords do not match.',exact=True).wait_for()
                page.locator('#password-confirm').fill(PASSWORD)
                page.get_by_role('button',name='Create admin account').click()
                page.locator('#logout').wait_for()
                page.goto(base+'#page=users')
                page.get_by_role('heading',name='User management',exact=True).wait_for()
                page.locator('#add-username').fill('member')
                page.locator('#new-password').fill(PASSWORD)
                page.locator('#password-confirm').fill(PASSWORD)
                page.get_by_role('button',name='Create user').click()
                page.get_by_role('cell',name='member',exact=True).first.wait_for()
                page.screenshot(path='local/accounts-users.png',full_page=True)
                page.locator('#logout').click()
                page.get_by_role('heading',name='Sign in to your workspace').wait_for()
                page.locator('#username').fill('member');page.locator('#password').fill('wrong-password')
                page.get_by_role('button',name='Sign in',exact=True).click()
                page.get_by_text('Incorrect username or password.',exact=True).wait_for()
                page.locator('#password').fill(PASSWORD)
                page.get_by_role('button',name='Sign in',exact=True).click()
                page.get_by_role('heading',name='Change password',exact=True).wait_for()
                page.locator('#current-password').fill(PASSWORD)
                page.locator('#new-password').fill('member-changed-password-123')
                page.locator('#password-confirm').fill('member-changed-password-123')
                page.get_by_role('button',name='Change password and continue').click()
                page.wait_for_selector('#auth-screen',state='detached')
                assert page.locator('#nav a[href="#page=users"]').count()==0
                assert page.request.get(base+'api/auth/users').status==403
                page.goto(base+'#page=account')
                page.get_by_role('heading',name='My account',exact=True).wait_for()
                page.locator('#current-password').fill('member-changed-password-123')
                page.locator('#new-password').fill('member-final-password-123')
                page.locator('#password-confirm').fill('member-final-password-123')
                page.get_by_role('button',name='Change password',exact=True).click()
                page.get_by_text('Password changed.',exact=True).wait_for()
                page.locator('#logout').click()
                page.get_by_role('heading',name='Sign in to your workspace').wait_for()
                page.set_viewport_size({'width':375,'height':812})
                page.screenshot(path='local/accounts-login-mobile.png',full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#username').fill('admin');page.locator('#password').fill(PASSWORD)
                page.get_by_role('button',name='Sign in',exact=True).click()
                page.wait_for_selector('#auth-screen',state='detached')
                page.set_viewport_size({'width':1440,'height':1000})
                page.goto(base+'#page=users')
                row=page.locator('section').first.get_by_role('row').filter(has=page.get_by_role('cell',name='member',exact=True))
                row.get_by_role('button',name='Manage',exact=True).click()
                page.locator('#edit-disabled').select_option('true')
                page.get_by_role('button',name='Save changes').click()
                page.get_by_role('cell',name='Disabled',exact=True).wait_for()
                assert not errors, errors
                browser.close()
                print('Accounts browser passed: setup/login/logout, forced/self password changes, role boundaries, disable, desktop/mobile.')
        finally:
            server.should_exit=True;thread.join(timeout=10)


if __name__=='__main__':main()
