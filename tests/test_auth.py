import hashlib
import hmac
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from geppetto.app import create_app

H = {'X-Geppetto-Request':'1'}
PASSWORD = 'abcdefghij'


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.app = create_app(self.home, 'catalog', False, token='machine-secret')
        self.client = TestClient(self.app)
        self.key = (self.home/'initial-admin-key').read_text()

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def post(self, path, body, client=None):
        return (client or self.client).post('/api/'+path, json=body, headers=H)

    def setup_admin(self):
        response = self.post('auth/setup', {'username':'Admin','password':PASSWORD,'setup_key':self.key})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['user']

    def login(self, name='admin', password=PASSWORD, client=None):
        return self.post('login', {'username':name,'password':password}, client)

    def new_user(self):
        r = self.post('auth/users', {'username':'member','password':PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_default_secure_and_legacy_cookie_rejected(self):
        self.assertEqual(self.client.get('/api/status').status_code, 401)
        self.assertEqual(self.post('login', {'token':'machine-secret'}).status_code, 401)
        old = hmac.new(b'machine-secret',b'geppetto-session-v1',hashlib.sha256).hexdigest()
        self.client.cookies.set('geppetto_session', old)
        self.assertEqual(self.client.get('/api/status').status_code, 401)
        self.assertEqual(self.client.get('/api/status',headers={'Authorization':'Bearer machine-secret'}).status_code, 200)
        with tempfile.TemporaryDirectory() as home:
            client = TestClient(create_app(home,'catalog',False,token=''))
            self.assertEqual(client.get('/api/status').status_code,401)
            client.close()

    def test_setup_is_one_time_and_secret_required(self):
        self.assertTrue(self.client.get('/api/auth/status').json()['setup_required'])
        self.assertEqual(self.post('auth/setup',{'username':'admin','password':PASSWORD,'setup_key':'bad'}).status_code,403)
        user = self.setup_admin()
        self.assertEqual(user['username'],'admin')
        self.assertNotIn('password_hash',user)
        self.assertFalse((self.home/'initial-admin-key').exists())
        self.assertFalse(self.client.get('/api/auth/status').json()['setup_required'])
        self.assertEqual(self.post('auth/setup',{'username':'second','password':PASSWORD,'setup_key':self.key}).status_code,409)
        with self.app.state.service.store.connect() as db:
            stored=db.execute('SELECT password_hash FROM auth_users').fetchone()[0]
            self.assertTrue(stored.startswith('$argon2id$'))
            session=db.execute('SELECT hash FROM auth_sessions').fetchone()[0]
            self.assertNotEqual(session,self.client.cookies.get('geppetto_session'))

    def test_setup_rejects_short_password(self):
        response = self.post('auth/setup', {
            'username': 'admin', 'password': 'abcdefghi', 'setup_key': self.key,
        })
        self.assertEqual(response.status_code, 422)
        self.assertTrue(self.client.get('/api/auth/status').json()['setup_required'])
        self.setup_admin()
        self.assertEqual(self.login().status_code, 200)

    def test_csrf_and_secure_cookie(self):
        self.assertEqual(self.client.post('/api/auth/setup',json={}).status_code,403)
        self.assertEqual(self.client.post('/api/login',json={},headers={**H,'Origin':'https://evil.test'}).status_code,403)
        with patch.dict('os.environ',{'GEPPETTO_SECURE_COOKIE':'1'}):
            self.setup_admin()
            response = self.login()
            cookie=response.headers['set-cookie']
            for flag in ('HttpOnly','Secure','SameSite=strict','Max-Age=43200'):
                self.assertIn(flag,cookie)

    def test_logout_and_password_revoke_all_sessions(self):
        self.setup_admin()
        other=TestClient(self.app)
        self.assertEqual(self.login(client=other).status_code,200)
        self.assertEqual(self.post('auth/password',{'current_password':'wrong','password':'new-password-123'}).status_code,403)
        r=self.post('auth/password',{'current_password':PASSWORD,'password':'new-password-123'})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(other.get('/api/status').status_code,401)
        cookie=self.client.cookies.get('geppetto_session')
        self.assertEqual(self.post('logout',{}).status_code,200)
        self.client.cookies.set('geppetto_session',cookie)
        self.assertEqual(self.client.get('/api/status').status_code,401)
        other.close()

    def test_user_must_change_password_and_admin_boundaries(self):
        self.setup_admin();member=self.new_user();other=TestClient(self.app)
        self.assertEqual(self.login('member',client=other).status_code,200)
        self.assertEqual(other.get('/api/status').status_code,403)
        self.assertEqual(other.get('/api/auth/me').status_code,200)
        self.assertEqual(self.post('auth/password',{'current_password':PASSWORD,'password':'member-new-password'},other).status_code,200)
        self.assertEqual(other.get('/api/status').status_code,200)
        for path in ('auth/users','auth/audit','sdk/tokens'):
            self.assertEqual(other.get('/api/'+path).status_code,403,path)
        for path in ('auth/users','clusters','whales','whales/node/execution','datasets/data/locations','integrations/sync'):
            self.assertEqual(self.post(path,{},other).status_code,403,path)
        self.assertEqual(self.client.patch('/api/auth/users/'+member['id'],json={'disabled':True},headers=H).status_code,200)
        self.assertEqual(other.get('/api/status').status_code,401)
        self.assertEqual(self.login('member','member-new-password',other).status_code,401)
        other.close()

    def test_last_admin_and_reset(self):
        admin=self.setup_admin();member=self.new_user()
        for body in ({'disabled':True},{'role':'operator'}):
            self.assertEqual(self.client.patch('/api/auth/users/'+admin['id'],json=body,headers=H).status_code,409)
        other=TestClient(self.app);self.login('member',client=other)
        r=self.client.patch('/api/auth/users/'+member['id'],json={'password':'reset-password-123'},headers=H)
        self.assertEqual(r.status_code,200)
        self.assertEqual(other.get('/api/auth/me').status_code,401)
        self.assertEqual(self.login('member','reset-password-123',other).status_code,200)
        self.assertTrue(other.get('/api/auth/me').json()['user']['must_change_password'])
        self.assertEqual(self.post('auth/users',{'username':'MEMBER','password':PASSWORD}).status_code,409)
        other.close()

    def test_persistent_session_expiration_and_rate_limit(self):
        self.setup_admin();cookie=self.client.cookies.get('geppetto_session')
        restarted=TestClient(create_app(self.home,'catalog',False,token='machine-secret'))
        restarted.cookies.set('geppetto_session',cookie)
        self.assertEqual(restarted.get('/api/status').status_code,200)
        with self.app.state.service.store.connect() as db:
            db.execute('UPDATE auth_sessions SET expires=0')
        self.assertEqual(restarted.get('/api/status').status_code,401)
        for _ in range(10):
            self.assertEqual(self.login('missing','bad').status_code,401)
        self.assertEqual(self.login('missing','bad').status_code,429)
        self.assertEqual(self.login('missing','bad',restarted).status_code,429)
        restarted.close()

    def test_setup_race_one_admin_wins(self):
        body={'username':'admin','password':PASSWORD,'setup_key':self.key}
        def attempt():
            with TestClient(self.app) as client:
                return client.post('/api/auth/setup',json=body,headers=H).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:attempt(),range(2)))
        self.assertEqual(sorted(results),[200,409])
        self.assertEqual(len(self.app.state.accounts.list()),1)


if __name__ == '__main__':
    unittest.main()
