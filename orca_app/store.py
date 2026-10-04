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
            db.execute('CREATE TABLE IF NOT EXISTS ordering (kind TEXT PRIMARY KEY, ids TEXT NOT NULL)')

        if os.name != 'nt':
            os.chmod(self.path, 0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def all(self, kind, items=None):
        with self.connect() as db:
            items = list(items) if items is not None else [json.loads(r[0]) for r in db.execute('SELECT value FROM records WHERE kind=? ORDER BY rowid', (kind,))]
            order = db.execute('SELECT ids FROM ordering WHERE kind=?', (kind,)).fetchone()
            if order:
                positions = {key: index for index, key in enumerate(json.loads(order[0]))}
                items.sort(key=lambda item: positions.get(item['id'], len(positions)))
            return items

    def reorder(self, kind, ids, allowed_ids=None):
        if not isinstance(ids, list) or any(not isinstance(key, str) for key in ids):
            raise ValueError('Order must be a list of item IDs.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = set(allowed_ids) if allowed_ids is not None else {row[0] for row in db.execute('SELECT id FROM records WHERE kind=?', (kind,))}
            if len(ids) != len(existing) or set(ids) != existing:
                raise ValueError('Items changed. Refresh and try reordering again.')
            db.execute('INSERT INTO ordering VALUES (?,?) ON CONFLICT(kind) DO UPDATE SET ids=excluded.ids',
                       (kind, json.dumps(ids)))

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
