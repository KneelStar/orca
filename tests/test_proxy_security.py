"""Client proxy boundaries with disposable HTTP fixtures and no host actions."""
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from orca_app.proxy import RESPONSE_LIMIT, proxy_request


TOKEN = 'test-client-token-that-must-not-appear-in-argv'


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.paths.append(self.path)
        self.server.authorization.append(self.headers.get('Authorization'))
        try:
            if self.path == '/slow-headers':
                self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Drip: ')
                while not self.server.stop.wait(.01):
                    self.wfile.write(b'x')
                    self.wfile.flush()
                return
            if self.path == '/slow-body':
                self.send_response(200)
                self.send_header('Content-Length', str(RESPONSE_LIMIT))
                self.end_headers()
                while not self.server.stop.wait(.01):
                    self.wfile.write(b'x')
                    self.wfile.flush()
                return
            status = 200
            body = b'{"ok":true}'
            headers = {}
            if self.path == '/redirect':
                status = 302
                headers['Location'] = '/should-not-be-followed'
            elif self.path == '/unauthorized':
                status = 401
                body = b'private upstream authentication detail'
            elif self.path == '/forbidden':
                status = 403
            elif self.path == '/failure':
                status = 503
                body = b'{"error":"temporarily unavailable"}'
            elif self.path == '/invalid':
                body = b'private upstream detail, not JSON'
            elif self.path == '/deep-json':
                body = b'[' * 10000 + b'0' + b']' * 10000
            elif self.path == '/oversized':
                body = b'{"value":"' + b'x' * RESPONSE_LIMIT + b'"}'
            elif self.path == '/compressed-oversized':
                headers['Content-Encoding'] = 'gzip'
                body = gzip.compress(b'{"value":"' + b'x' * (2 * RESPONSE_LIMIT) + b'"}')
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.close_connection = True


class ProxySecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), ProxyHandler)
        cls.server.daemon_threads = True
        cls.server.stop = threading.Event()
        cls.server.paths = []
        cls.server.authorization = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.address = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def request(self, path, read_timeout=1):
        return proxy_request('GET', self.address + path, TOKEN, None, read_timeout)

    def test_success_uses_saved_token_and_ignores_proxy_environment(self):
        env = {'HTTP_PROXY': 'http://127.0.0.1:1', 'http_proxy': 'http://127.0.0.1:1',
               'NO_PROXY': '', 'no_proxy': ''}
        with patch.dict(os.environ, env):
            self.assertEqual(self.request('/ok'), ({'ok': True}, 200))
        self.assertEqual(self.server.authorization[-1], 'Bearer ' + TOKEN)

    def test_remote_non_auth_status_is_preserved(self):
        self.assertEqual(self.request('/failure'), ({'error': 'temporarily unavailable'}, 503))

    def test_redirect_is_not_followed(self):
        with self.assertRaisesRegex(RuntimeError, 'redirected'):
            self.request('/redirect')
        self.assertNotIn('/should-not-be-followed', self.server.paths)

    def test_remote_auth_failures_do_not_expose_upstream_response(self):
        for path in ('/unauthorized', '/forbidden'):
            with self.subTest(path=path):
                with self.assertRaisesRegex(RuntimeError, '^Client authentication failed'):
                    self.request(path)

    def test_oversized_decoded_response_is_rejected(self):
        for path in ('/oversized', '/compressed-oversized'):
            with self.subTest(path=path):
                with self.assertRaisesRegex(RuntimeError, 'size limit'):
                    self.request(path)

    def test_invalid_response_is_generic(self):
        for path in ('/invalid', '/deep-json'):
            with self.subTest(path=path):
                with self.assertRaisesRegex(RuntimeError, '^Client returned an invalid response\\.$'):
                    self.request(path)

    def test_parent_rejects_worker_json_beyond_recursion_limit(self):
        response = subprocess.CompletedProcess([], 0,
            '{"value":' + '[' * 10000 + '0' + ']' * 10000 + ',"status":200}', '')
        with patch('orca_app.image_updates.run_bounded', return_value=response):
            with self.assertRaisesRegex(RuntimeError, '^Client returned an invalid response\\.$'):
                self.request('/ok')

    def test_whole_request_deadline_stops_slow_headers_and_body(self):
        for path in ('/slow-headers', '/slow-body'):
            with self.subTest(path=path):
                started = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, 'time limit'):
                    self.request(path, read_timeout=.2)
                elapsed = time.monotonic() - started
                self.assertGreater(elapsed, 4.5)
                self.assertLess(elapsed, 7)

    def test_credentials_only_travel_via_private_stdin(self):
        def capture(args, **kwargs):
            self.assertNotIn(TOKEN, repr(args))
            self.assertIn('-I', args)
            self.assertFalse(any(key.upper().startswith('ORCA_') for key in kwargs['env']))
            self.assertNotIn(TOKEN, repr(kwargs['env']))
            stdin = kwargs['stdin']
            self.assertEqual(os.fstat(stdin.fileno()).st_mode & 0o777, 0o600)
            data = json.load(stdin)
            self.assertEqual(data['token'], TOKEN)
            self.assertEqual(data['payload'], {'value': 'test'})
            self.assertEqual(kwargs['timeout'], 15)
            return subprocess.CompletedProcess(args, 0, '{"value":{},"status":200}', '')
        with patch.dict(os.environ, {'ORCA_CLIENT_TOKEN': TOKEN, 'ORCA_SESSION_SECRET': 'private'}):
            with patch('orca_app.image_updates.run_bounded', side_effect=capture):
                self.assertEqual(proxy_request('POST', self.address, TOKEN, {'value': 'test'}, 10), ({}, 200))


if __name__ == '__main__':
    unittest.main()
