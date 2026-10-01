"""Consistent SQLite snapshot; includes committed WAL transactions."""
import argparse
from pathlib import Path
import sqlite3
parser=argparse.ArgumentParser()
parser.add_argument('database',type=Path)
parser.add_argument('destination',type=Path)
a=parser.parse_args()
if a.destination.exists(): parser.error('Refusing to overwrite an existing backup')
with sqlite3.connect(f'file:{a.database.resolve()}?mode=ro',uri=True) as source,sqlite3.connect(a.destination) as target:
    source.backup(target)
    assert target.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
print(a.destination)
