"""Regressions for session revocation and unambiguous proxy destinations."""
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orca_app.app import create_app


class AppSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = dict(TESTING=True, ROLE='orchestrator',
                           ADMIN_PASSWORD='security-test-password', SECRET_KEY='s' * 48,
                           CLIENT_TOKEN='t' * 48, DATABASE=str(Path(self.temp.name) / 'orca.sqlite3'))
        self.app = create_app(self.config)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def login(self, client=None):
        client = client or self.client
        csrf = client.get('/api/session').json['csrf']
        response = client.post('/api/login', json={'password': self.config['ADMIN_PASSWORD']},
                               headers={'X-CSRF-Token': csrf})
        self.assertEqual(response.status_code, 200)
        return {'X-CSRF-Token': response.json['csrf']}

    def replay(self, cookie, app=None, cookie_name='orca_orchestrator'):
        client = (app or self.app).test_client()
        client.set_cookie(cookie_name, cookie)
        return client

    def test_logout_revokes_copied_cookie(self):
        headers = self.login()
        cookie = self.client.get_cookie('orca_orchestrator').value
        self.assertEqual(self.client.post('/api/logout', headers=headers).status_code, 200)
        self.assertEqual(self.replay(cookie).get('/api/clients').status_code, 401)

    def test_password_change_revokes_existing_session(self):
        self.login()
        cookie = self.client.get_cookie('orca_orchestrator').value
        changed = create_app(dict(self.config, ADMIN_PASSWORD='different-admin-password'))
        self.assertEqual(self.replay(cookie, changed).get('/api/clients').status_code, 401)

    def test_live_session_survives_restart_with_same_credentials(self):
        self.login()
        cookie = self.client.get_cookie('orca_orchestrator').value
        restarted = create_app(self.config)
        self.assertEqual(self.replay(cookie, restarted).get('/api/clients').status_code, 200)

    def test_sessions_do_not_cross_roles_or_databases(self):
        self.login()
        cookie = self.client.get_cookie('orca_orchestrator').value
        other = create_app(dict(self.config, DATABASE=str(Path(self.temp.name) / 'other.sqlite3')))
        self.assertEqual(self.replay(cookie, other).get('/api/clients').status_code, 401)
        with patch.dict('os.environ', {'ORCA_EXECUTION_MODE': 'local'}):
            host = create_app(dict(self.config, ROLE='client'))
        self.assertEqual(self.replay(cookie, host, 'orca_client').get('/api/host/actions').status_code, 401)

    def test_reauthentication_revokes_previous_cookie(self):
        self.login()
        previous = self.client.get_cookie('orca_orchestrator').value
        self.login()
        self.assertEqual(self.replay(previous).get('/api/clients').status_code, 401)
        self.assertEqual(self.client.get('/api/clients').status_code, 200)

    def test_legacy_or_cookie_only_admin_claim_is_rejected(self):
        with self.client.session_transaction() as session:
            session.update(admin=True, csrf='fake', sid='not-issued-by-server')
        self.assertEqual(self.client.get('/api/clients').status_code, 401)

    def test_session_expires_even_if_cookie_is_refreshed(self):
        self.login()
        with self.client.session_transaction() as session:
            identifier = session['sid']
        store = self.app.extensions['store']
        records = store.all('admin-sessions')
        self.assertEqual(records[0]['id'], hashlib.sha256(identifier.encode()).hexdigest())
        self.assertNotIn(identifier, str(records))
        expires = records[0]['expires']
        with patch('orca_app.app.time.time', return_value=expires - 1):
            self.assertEqual(self.client.get('/api/clients').status_code, 200)
        with patch('orca_app.app.time.time', return_value=expires + 1):
            self.assertEqual(self.client.get('/api/clients').status_code, 401)
        self.assertEqual(store.all('admin-sessions'), [])

    def test_expired_sessions_pruned_without_removing_other_records(self):
        store = self.app.extensions['store']
        now = time.time()
        store.put('admin-sessions', dict(id='expired', expires=now - 1))
        store.put('admin-sessions', dict(id='live', expires=now + 100))
        store.put('actions', dict(id='preserved', expires=now - 1))
        create_app(self.config)
        self.assertIsNone(store.get('admin-sessions', 'expired'))
        self.assertIsNotNone(store.get('admin-sessions', 'live'))
        self.assertIsNotNone(store.get('actions', 'preserved'))

    def test_session_info_recovers_after_expiry_or_password_change(self):
        self.login()
        store = self.app.extensions['store']
        record = store.all('admin-sessions')[0]
        record['expires'] = 0
        store.put('admin-sessions', record)
        response = self.client.get('/api/session')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['admin'])
        self.assertTrue(response.json['csrf'])
        self.assertEqual(self.client.post('/api/login', json={'password': self.config['ADMIN_PASSWORD']},
                         headers={'X-CSRF-Token': response.json['csrf']}).status_code, 200)
        cookie = self.client.get_cookie('orca_orchestrator').value
        changed = create_app(dict(self.config, ADMIN_PASSWORD='different-admin-password'))
        response = self.replay(cookie, changed).get('/api/session')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['admin'])
        self.assertTrue(response.json['csrf'])

    def test_ambiguous_urls_and_header_tokens_are_rejected(self):
        headers = self.login()
        invalid = ['http://example.com\\@other.example', 'http://example.com\\other',
                   'http://exa\nmple.com', 'http://exa\tmple.com',
                   'http://%65xample.com', 'http://example.com:0',
                   'http://user@example.com', 'http://@example.com', 'http://[::1]other.example',
                   'http://example.com:', 'https://example.com?', 'https://example.com#']
        invalid += ['http://', 'https:///missing-host', 'javascript:alert(1)', 'file:///etc/passwd']
        for address in invalid:
            with self.subTest(address=address):
                response = self.client.post('/api/clients', headers=headers,
                                            json=dict(name='Invalid', url=address, token='t' * 48))
                self.assertEqual(response.status_code, 400)
        for address in ['http://127.0.0.1:8001', 'https://orca.local/', 'http://[::1]:8001',
                        'https://m\u00fcnchen.example']:
            with self.subTest(address=address):
                self.assertEqual(self.client.post('/api/clients', headers=headers,
                                 json=dict(name='Valid', url=address, token='t' * 48)).status_code, 200)
        for token in ['t' * 32 + '\r\nInjected: value', 't' * 32 + ' space', 't' * 32 + '\u2603']:
            with self.subTest(token=repr(token)):
                self.assertEqual(self.client.post('/api/clients', headers=headers,
                                 json=dict(name='Invalid', url='http://127.0.0.1:8001', token=token)).status_code, 400)

    def test_proxy_preserves_status_and_session_and_enforces_allowlist(self):
        headers = self.login()
        saved = self.client.post('/api/clients', headers=headers,
                                 json=dict(name='Client', url='http://127.0.0.1:8001', token='t' * 48)).json
        base = '/api/clients/' + saved['id'] + '/remote/'
        with patch('orca_app.app.proxy_request', return_value=({'error': 'Busy'}, 409)) as proxy:
            response = self.client.post(base + 'actions', headers=headers, json={'name': 'Test'})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json, {'error': 'Busy'})
            proxy.assert_called_once_with('POST', 'http://127.0.0.1:8001/api/host/actions',
                                          't' * 48, {'name': 'Test'}, 10)
        with patch('orca_app.app.proxy_request', return_value=({'containers': []}, 200)) as proxy:
            self.assertEqual(self.client.get(base + 'docker').status_code, 200)
            self.assertEqual(proxy.call_args.args[-1], 65)
        with patch('orca_app.app.proxy_request', side_effect=RuntimeError('Client authentication failed.')):
            self.assertEqual(self.client.get(base + 'metrics').status_code, 502)
            self.assertTrue(self.client.get('/api/session').json['admin'])
        with patch('orca_app.app.proxy_request') as proxy:
            self.assertEqual(self.client.post(base + 'login', headers=headers, json={}).status_code, 404)
            self.assertEqual(self.client.get(base + 'actions/../session').status_code, 404)
            proxy.assert_not_called()


if __name__ == '__main__':
    unittest.main()
