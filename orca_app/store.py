import json
import os
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, value TEXT, PRIMARY KEY(kind,id))')

        if os.name != 'nt':
            os.chmod(self.path, 0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def all(self, kind):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT value FROM records WHERE kind=? ORDER BY rowid', (kind,))]

    def get(self, kind, key):
        with self.connect() as db:
            row = db.execute('SELECT value FROM records WHERE kind=? AND id=?', (kind, key)).fetchone()
            return json.loads(row[0]) if row else None

    def put(self, kind, item):
        with self.connect() as db:
            db.execute('INSERT INTO records VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET value=excluded.value',
                       (kind, item['id'], json.dumps(item)))
        return item

    def delete(self, kind, key):
        with self.connect() as db:
            return db.execute('DELETE FROM records WHERE kind=? AND id=?', (kind, key)).rowcount > 0

    def prune_jobs(self, cutoff):
        # Delete only finished run records; action definitions and running jobs survive.
        with self.connect() as db:
            return db.execute("""
                DELETE FROM records WHERE kind = 'jobs'
                AND json_extract(value, '$.status') IN
                    ('succeeded', 'failed', 'timed_out', 'interrupted')
                AND COALESCE(json_extract(value, '$.finished'),
                             json_extract(value, '$.started')) < ?
            """, (cutoff,)).rowcount
