import json
import os
import sqlite3
import stat
from pathlib import Path


class _StoreConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Store:
    def __init__(self, path):
        self.path = str(Path(path).absolute())
        self.uri = Path(self.path).as_uri() + '?mode=rw'
        Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Set restrictive permissions before SQLite can write credentials or
        # create journal files. chmod after connecting leaves an exposure window.
        self._secure_file(create=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, value TEXT, PRIMARY KEY(kind,id))')
            db.execute('CREATE TABLE IF NOT EXISTS ordering (kind TEXT PRIMARY KEY, ids TEXT NOT NULL)')

    def _secure_file(self, create=False):
        flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        if create:
            flags |= os.O_CREAT
        descriptor = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('Database must be a regular file.')
            if os.name != 'nt':
                if info.st_uid != os.geteuid() or info.st_nlink != 1:
                    raise ValueError('Database must be owned by the service user and have no hard links.')
                os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    def connect(self):
        self._secure_file()
        # Never silently recreate a removed database with SQLite's default mode.
        return sqlite3.connect(self.uri, timeout=10, uri=True, factory=_StoreConnection)

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

    def prune_sessions(self, now):
        with self.connect() as db:
            return db.execute("""
                DELETE FROM records WHERE kind = 'admin-sessions'
                AND json_extract(value, '$.expires') <= ?
            """, (now,)).rowcount

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
