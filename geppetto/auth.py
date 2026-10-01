"""Local accounts and revocable sessions, isolated from the public catalog."""
import hashlib
import hmac
import os
import re
import secrets
import time
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import HTTPException

PH = PasswordHasher()
DUMMY = PH.hash(secrets.token_urlsafe(32))
SESSION_SECONDS = 12 * 3600


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def public(row):
    return dict(zip(('id', 'username', 'role', 'disabled', 'must_change_password', 'created_at'),
                    (row[0], row[1], row[3], bool(row[4]), bool(row[5]), row[6])))


def password(value):
    if not isinstance(value, str) or not 10 <= len(value) <= 128:
        raise HTTPException(422, 'Password must contain 10–128 characters.')
    return value


def username(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{2,39}', value):
        raise HTTPException(422, 'Username must contain 3–40 letters, digits, periods, underscores, or hyphens, and begin with a letter or digit.')
    return value.lower()


class Accounts:
    def __init__(self, store, home):
        self.store = store
        self.setup_file = Path(home) / 'initial-admin-key'
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS auth_users (
                    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
                    role TEXT NOT NULL, disabled INTEGER NOT NULL, must_change_password INTEGER NOT NULL,
                    created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES auth_users(id), expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS auth_limits (key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS auth_audit (
                    id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS auth_setup (id INTEGER PRIMARY KEY CHECK(id=1), hash TEXT NOT NULL);
            ''')
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM auth_users LIMIT 1').fetchone() and not db.execute('SELECT 1 FROM auth_setup').fetchone():
                key = secrets.token_urlsafe(32)
                fd = os.open(self.setup_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w') as f:
                    f.write(key)
                db.execute('INSERT INTO auth_setup VALUES(1,?)', (digest(key),))

    def configured(self):
        with self.store.connect() as db:
            return bool(db.execute('SELECT 1 FROM auth_users LIMIT 1').fetchone())

    @staticmethod
    def audit(db, actor, action, target):
        db.execute('INSERT INTO auth_audit(at,actor,action,target) VALUES(?,?,?,?)',
                   (time.time(), actor, action, target))

    def limit(self, peer, name):
        now = time.time()
        blocked = False
        with self.store.connect() as db:
            db.execute('DELETE FROM auth_limits WHERE expires < ?', (now,))
            for key, maximum in [('ip:'+peer, 30), ('name:'+name[:100], 10)]:
                key = digest(key)
                row = db.execute('SELECT count FROM auth_limits WHERE key=?', (key,)).fetchone()
                if row and row[0] >= maximum:
                    blocked = True
                else:
                    db.execute('INSERT INTO auth_limits VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=count+1', (key, now+900))
        if blocked:
            raise HTTPException(429, 'Too many login attempts. Try again in 15 minutes.', headers={'Retry-After': '900'})

    def issue(self, db, user):
        now = time.time()
        db.execute('DELETE FROM auth_sessions WHERE expires < ?', (now,))
        # Keep at most ten concurrent browser sessions per account.
        db.execute('DELETE FROM auth_sessions WHERE hash IN (SELECT hash FROM auth_sessions WHERE user_id=? ORDER BY expires DESC LIMIT -1 OFFSET 9)', (user[0],))
        secret = secrets.token_urlsafe(32)
        db.execute('INSERT INTO auth_sessions VALUES(?,?,?)', (digest(secret), user[0], now+SESSION_SECONDS))
        return public(user), secret

    def setup(self, body, peer):
        self.limit(peer, 'initial-admin')
        name = username(body.get('username'))
        raw = password(body.get('password'))
        key = body.get('setup_key', '')
        with self.store.connect() as db:
            if db.execute('SELECT 1 FROM auth_users LIMIT 1').fetchone():
                raise HTTPException(409, 'Administrator setup is already complete.')
            row = db.execute('SELECT hash FROM auth_setup WHERE id=1').fetchone()
            if not isinstance(key, str) or not row or not hmac.compare_digest(row[0], digest(key)):
                raise HTTPException(403, 'Use the setup link provided by your server administrator.')
            user = (secrets.token_hex(16), name, PH.hash(raw), 'admin', 0, 0, time.time())
            db.execute('INSERT INTO auth_users VALUES(?,?,?,?,?,?,?)', user)
            db.execute('DELETE FROM auth_setup')
            self.audit(db, user[0], 'setup', user[0])
            result = self.issue(db, user)
        self.setup_file.unlink(missing_ok=True)
        return result

    def login(self, body, peer):
        name = str(body.get('username', '')).lower()[:128]
        raw = body.get('password', '')
        self.limit(peer, name)
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM auth_users WHERE username=?', (name,)).fetchone()
            valid = False
            if isinstance(raw, str) and len(raw) <= 128:
                try:
                    valid = PH.verify(row[2] if row else DUMMY, raw)
                except (VerificationError, InvalidHashError):
                    pass
            if valid and row and not row[4]:
                if PH.check_needs_rehash(row[2]):
                    db.execute('UPDATE auth_users SET password_hash=? WHERE id=?', (PH.hash(raw), row[0]))
                db.execute('DELETE FROM auth_limits WHERE key=?', (digest('name:'+name[:100]),))
                self.audit(db, row[0], 'login', row[0])
                return self.issue(db, row)
            self.audit(db, 'anonymous', 'login_failed', name)
        raise HTTPException(401, 'Incorrect username or password.')

    def resolve(self, secret):
        if not secret:
            return None
        with self.store.connect() as db:
            row = db.execute('SELECT u.* FROM auth_users u JOIN auth_sessions s ON u.id=s.user_id WHERE s.hash=? AND s.expires>? AND u.disabled=0', (digest(secret), time.time())).fetchone()
            return public(row) if row else None

    def logout(self, secret):
        with self.store.connect() as db:
            db.execute('DELETE FROM auth_sessions WHERE hash=?', (digest(secret),))

    def list(self):
        with self.store.connect() as db:
            return [public(row) for row in db.execute('SELECT * FROM auth_users ORDER BY created_at')]

    def create(self, body, actor):
        name = username(body.get('username'))
        role = body.get('role', 'operator')
        if role not in ('admin', 'operator'):
            raise HTTPException(422, 'Unsupported role.')
        hashed = PH.hash(password(body.get('password')))
        with self.store.connect() as db:
            if db.execute('SELECT 1 FROM auth_users WHERE username=?', (name,)).fetchone():
                raise HTTPException(409, 'Username is already in use.')
            row = (secrets.token_hex(16), name, hashed, role, 0, 1, time.time())
            db.execute('INSERT INTO auth_users VALUES(?,?,?,?,?,?,?)', row)
            self.audit(db, actor, 'user_created', row[0])
            return public(row)

    def update(self, user_id, body, actor):
        if not body or set(body)-{'role', 'disabled', 'password'}:
            raise HTTPException(422, 'Check the role, enabled status, and password changes.')
        hashed = PH.hash(password(body['password'])) if 'password' in body else None
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM auth_users WHERE id=?', (user_id,)).fetchone()
            if not row:
                raise HTTPException(404, 'Account not found.')
            role, disabled = body.get('role', row[3]), body.get('disabled', bool(row[4]))
            if role not in ('admin', 'operator') or not isinstance(disabled, bool):
                raise HTTPException(422, 'Check account settings.')
            if row[3]=='admin' and not row[4] and (role!='admin' or disabled):
                if db.execute("SELECT count(*) FROM auth_users WHERE role='admin' AND disabled=0").fetchone()[0] <= 1:
                    raise HTTPException(409, 'The last active administrator cannot be disabled or demoted.')
            db.execute('UPDATE auth_users SET role=?,disabled=?,password_hash=?,must_change_password=? WHERE id=?',
                       (role, int(disabled), hashed or row[2], 1 if hashed else row[5], user_id))
            db.execute('DELETE FROM auth_sessions WHERE user_id=?', (user_id,))
            self.audit(db, actor, 'password_reset' if hashed else 'user_updated', user_id)
            return public(db.execute('SELECT * FROM auth_users WHERE id=?', (user_id,)).fetchone())

    def change_password(self, user_id, body, peer):
        self.limit(peer, 'password:'+user_id)
        raw = password(body.get('password'))
        current = body.get('current_password', '')
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM auth_users WHERE id=? AND disabled=0', (user_id,)).fetchone()
            try:
                if not row or not isinstance(current, str) or len(current)>128 or not PH.verify(row[2], current):
                    raise HTTPException(403, 'Incorrect current password.')
            except (VerificationError, InvalidHashError):
                raise HTTPException(403, 'Incorrect current password.')
            if current == raw:
                raise HTTPException(422, 'Choose a password different from your current password.')
            db.execute('UPDATE auth_users SET password_hash=?,must_change_password=0 WHERE id=?', (PH.hash(raw), user_id))
            db.execute('DELETE FROM auth_sessions WHERE user_id=?', (user_id,))
            self.audit(db, user_id, 'password_changed', user_id)
            return self.issue(db, db.execute('SELECT * FROM auth_users WHERE id=?', (user_id,)).fetchone())


def main():
    """Server-local recovery; never accepts passwords in process arguments."""
    import argparse
    import getpass
    from .storage import Store
    parser = argparse.ArgumentParser(description='Recover administrator password (local server only)')
    parser.add_argument('--home', required=True)
    parser.add_argument('--username', required=True)
    args = parser.parse_args()
    auth = Accounts(Store(Path(args.home)/'catalog.sqlite3'), args.home)
    users = [u for u in auth.list() if u['username']==username(args.username)]
    if not users:
        parser.error('Account not found.')
    raw = password(getpass.getpass('New temporary password: '))
    if raw != getpass.getpass('Confirm new temporary password: '):
        parser.error('Passwords do not match.')
    auth.update(users[0]['id'], {'password': raw, 'disabled': False}, 'local-recovery')
    print('Password updated. Change it on your next login.')


if __name__ == '__main__':
    main()
