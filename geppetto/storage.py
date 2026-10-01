"""SQLite catalog with transactional writes and durable scheduling/outbox state."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute('PRAGMA journal_mode=WAL')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > 1:
                raise ValueError('Database is newer than this application')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS documents (
                  kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
                  PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS requests (
                  key TEXT PRIMARY KEY, digest TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL);
                PRAGMA user_version=1;
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def put(db, kind, value):
        db.execute('INSERT INTO documents VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body',
                   (kind, value['id'], json.dumps(value, ensure_ascii=False, allow_nan=False)))

    @staticmethod
    def get(db, kind, id):
        row = db.execute('SELECT body FROM documents WHERE kind=? AND id=?', (kind, id)).fetchone()
        if not row:
            raise KeyError(f'{kind}: {id}')
        return json.loads(row[0])

    @staticmethod
    def all(db, kind):
        return [json.loads(row[0]) for row in db.execute('SELECT body FROM documents WHERE kind=? ORDER BY rowid DESC', (kind,))]
