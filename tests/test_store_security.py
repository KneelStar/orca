import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from waitress.adjustments import Adjustments
from waitress.parser import HTTPRequestParser

from orca_app.cli import run
from orca_app.store import Store


class StoreSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'orca.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    @unittest.skipIf(os.name == 'nt', 'POSIX file permissions')
    def test_database_is_private_before_sqlite_connects(self):
        original_connect = sqlite3.connect
        observed = []

        def inspect_permissions(*args, **kwargs):
            observed.append(stat.S_IMODE(self.path.stat().st_mode))
            return original_connect(*args, **kwargs)

        previous_umask = os.umask(0o022)
        try:
            with patch('orca_app.store.sqlite3.connect', side_effect=inspect_permissions):
                store = Store(self.path)
                store.put('clients', {'id': 'server', 'token': 'private-token'})
            self.assertTrue(observed)
            self.assertEqual(set(observed), {0o600})
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE records SET value = value || ' ' WHERE kind = 'clients'")
                journal = Path(str(self.path) + '-journal')
                self.assertTrue(journal.exists())
                self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)
        finally:
            os.umask(previous_umask)

    @unittest.skipIf(os.name == 'nt', 'POSIX file permissions')
    def test_existing_database_is_secured_before_reading(self):
        store = Store(self.path)
        store.put('clients', {'id': 'server', 'token': 'private-token'})
        self.path.chmod(0o644)
        reopened = Store(self.path)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(reopened.get('clients', 'server')['token'], 'private-token')

    def test_removed_database_is_not_recreated_by_reconnect(self):
        store = Store(self.path)
        self.path.unlink()
        with self.assertRaises((OSError, sqlite3.OperationalError)):
            store.connect()
        self.assertFalse(self.path.exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX links and file permissions')
    def test_database_symlink_cannot_modify_another_file(self):
        target = Path(self.temp.name) / 'other-file'
        target.write_text('unrelated data')
        target.chmod(0o644)
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            Store(self.path)
        self.assertEqual(target.read_text(), 'unrelated data')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_session_cleanup_preserves_other_records_and_unexpired_sessions(self):
        store = Store(self.path)
        for key, expires in (('old', 99), ('boundary', 100), ('active', 101)):
            store.put('admin-sessions', {'id': key, 'expires': expires})
        store.put('clients', {'id': 'server', 'expires': 1})
        store.put('jobs', {'id': 'job', 'expires': 1})
        self.assertEqual(store.prune_sessions(100), 2)
        self.assertEqual([item['id'] for item in store.all('admin-sessions')], ['active'])
        self.assertIsNotNone(store.get('clients', 'server'))
        self.assertIsNotNone(store.get('jobs', 'job'))

    def test_sqlite_uri_escapes_database_filename(self):
        path = Path(self.temp.name) / 'orca?mode=memory#data.sqlite3'
        Store(path).put('clients', {'id': 'server'})
        self.assertIsNotNone(Store(path).get('clients', 'server'))

    def test_connections_close_after_committing(self):
        store = Store(self.path)
        with store.connect() as db:
            db.execute("INSERT INTO records VALUES ('clients', 'server', '{\"id\":\"server\"}')")
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute('SELECT 1')
        self.assertEqual(store.get('clients', 'server'), {'id': 'server'})

    def test_connections_rollback_and_close_after_errors(self):
        store = Store(self.path)
        with self.assertRaisesRegex(ValueError, 'rollback'):
            with store.connect() as db:
                db.execute("INSERT INTO records VALUES ('clients', 'server', '{\"id\":\"server\"}')")
                raise ValueError('rollback')
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute('SELECT 1')
        self.assertIsNone(store.get('clients', 'server'))


class ServerBodyLimitTests(unittest.TestCase):
    def server_adjustments(self):
        app = SimpleNamespace(config={'ROLE': 'orchestrator', 'MAX_CONTENT_LENGTH': 65536})
        with patch('orca_app.cli.create_app', return_value=app), patch('orca_app.cli.serve') as serve:
            run()
        return Adjustments(**serve.call_args.kwargs)

    def test_server_rejects_oversized_upload_before_buffering_body(self):
        parser = HTTPRequestParser(self.server_adjustments())
        parser.received(b'POST /api/login HTTP/1.1\r\nHost: orca.local\r\n'
                        b'Content-Length: 524289\r\n\r\n')
        self.assertTrue(parser.completed)
        self.assertEqual(parser.error.code, 413)
        self.assertEqual(parser.body_bytes_received, 0)

    def test_server_rejects_chunked_upload_at_body_limit(self):
        parser = HTTPRequestParser(self.server_adjustments())
        parser.received(b'POST /api/login HTTP/1.1\r\nHost: orca.local\r\n'
                        b'Transfer-Encoding: chunked\r\n\r\n')
        chunk = b'2000\r\n' + b'x' * 8192 + b'\r\n'
        for _ in range(9):
            parser.received(chunk)
            if parser.completed:
                break
        self.assertTrue(parser.completed)
        self.assertEqual(parser.error.code, 413)
        self.assertLessEqual(parser.body_bytes_received, 65536 + len(chunk))

    def test_server_accepts_normal_json_requests(self):
        parser = HTTPRequestParser(self.server_adjustments())
        body = b'{"password":"example-password"}'
        parser.received(b'POST /api/login HTTP/1.1\r\nHost: orca.local\r\n'
                        b'Content-Type: application/json\r\nContent-Length: ' +
                        str(len(body)).encode() + b'\r\n\r\n')
        parser.received(body)
        self.assertTrue(parser.completed)
        self.assertIsNone(parser.error)


if __name__ == '__main__':
    unittest.main()
