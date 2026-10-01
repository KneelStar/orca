import json
import os
import shlex
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.serving import make_server

from orca_app.app import create_app
from orca_app.execution import Execution


class OrcaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'ORCA_EXECUTION_MODE': 'local'})
        self.env.start()
        self.config = dict(TESTING=True, ADMIN_PASSWORD='test-password-for-orca', SECRET_KEY='s'*48,
                           CLIENT_TOKEN='t'*48, DATABASE=str(Path(self.temp.name)/'orca.sqlite3'),
                           ADMIN_TITLE='My servers', PUBLIC_TITLE='Family links', ROLE='orchestrator')
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        self.csrf = self.client.get('/api/session').json['csrf']

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def login(self, client=None):
        client = client or self.client
        csrf = client.get('/api/session').json['csrf']
        response = client.post('/api/login', json={'password':self.config['ADMIN_PASSWORD']}, headers={'X-CSRF-Token':csrf})
        self.assertEqual(response.status_code, 200)
        return {'X-CSRF-Token':response.json['csrf']}

    def host(self):
        return create_app(dict(self.config, ROLE='client', DATABASE=str(Path(self.temp.name)/'host.sqlite3')))

    def test_public_does_not_reveal_management_or_private_shortcuts(self):
        headers=self.login()
        for visibility in ('admin','shared'):
            response=self.client.post('/api/shortcuts',headers=headers,json={'name':visibility,'url':'https://example.com','visibility':visibility})
            self.assertEqual(response.status_code,200)
        self.client.post('/api/logout',headers=headers,json={})
        self.assertEqual([s['name'] for s in self.client.get('/api/shortcuts').json],['shared'])
        for url in ('/api/clients','/api/clients/a/remote/metrics','/api/host/actions','/api/host/jobs'):
            self.assertEqual(self.client.get(url).status_code,401)
        data=self.client.get('/api/session').json
        self.assertEqual(data['public_title'],'Family links')
        self.assertIsNone(data['client_name'])

    def test_csrf_sessions_and_unsafe_urls(self):
        self.assertEqual(self.client.post('/api/login',json={'password':self.config['ADMIN_PASSWORD']}).status_code,403)
        headers=self.login()
        self.assertEqual(self.client.post('/api/shortcuts',json={}).status_code,403)
        for url in ('javascript:alert(1)','https://user:pass@example.com','file:///etc/passwd'):
            self.assertEqual(self.client.post('/api/shortcuts',headers=headers,json={'name':'bad','url':url}).status_code,400)
        self.assertEqual(self.client.post('/api/clients',headers=headers,json={'name':'bad','url':'http://example.com/path','token':'t'*48}).status_code,400)

    def test_persistence_and_no_token_in_client_response(self):
        headers=self.login()
        response=self.client.post('/api/clients',headers=headers,json={'name':'host','url':'http://127.0.0.1:9999','token':'private-token-'+'x'*40})
        self.assertEqual(response.status_code,200)
        self.assertNotIn('token',response.json)
        self.assertNotIn('private-token',self.client.get('/api/clients').text)
        reopened=create_app(self.config).test_client()
        self.login(reopened)
        self.assertEqual(reopened.get('/api/clients').json[0]['name'],'host')

    def test_host_requires_auth_and_collects_real_metrics(self):
        client=self.host().test_client()
        self.assertEqual(client.get('/api/host/metrics').status_code,401)
        self.assertEqual(client.get('/api/host/metrics',headers={'Authorization':'Bearer wrong'}).status_code,401)
        response=client.get('/api/host/metrics',headers={'Authorization':'Bearer '+'t'*48})
        self.assertEqual(response.status_code,200)
        self.assertIn('os',response.json)
        self.assertGreaterEqual(response.json['cpu'],0)
        self.assertLessEqual(response.json['ram'],100)
        self.assertEqual(client.get('/api/clients',headers=self.login(client)).status_code,404)

    def wait_job(self,client,key,headers):
        for _ in range(100):
            job=client.get('/api/host/jobs/'+key,headers=headers).json
            if job['status']!='running':return job
            time.sleep(.05)
        self.fail('Job did not finish')

    def test_action_output_failure_and_timeout(self):
        host=self.host();client=host.test_client();headers=self.login(client)
        commands=[('printf hello','succeeded','hello',5),('printf failed; exit 3','failed','failed',5),('sleep 5','timed_out','time limit',1)]
        for command,status,output,timeout in commands:
            action=client.post('/api/host/actions',headers=headers,json={'name':'Test','command':command,'cwd':self.temp.name,'timeout':timeout})
            self.assertEqual(action.status_code,200)
            response=client.post('/api/host/actions/'+action.json['id']+'/run',headers=headers,json={})
            self.assertEqual(response.status_code,202)
            job=self.wait_job(client,response.json['id'],headers)
            self.assertEqual(job['status'],status)
            self.assertIn(output,job['output'])
        history=client.get('/api/host/jobs',headers=headers).json
        self.assertEqual(len(history),3)
        self.assertNotIn('output',history[0])

    def test_output_is_live_and_concurrent_actions_are_rejected(self):
        host=self.host();client=host.test_client();headers=self.login(client)
        action=client.post('/api/host/actions',headers=headers,json={'name':'Live','command':'printf first; sleep 1; printf second','timeout':5}).json
        path='/api/host/actions/'+action['id']+'/run'
        job=client.post(path,headers=headers,json={}).json
        self.assertEqual(client.post(path,headers=headers,json={}).status_code,409)
        time.sleep(.2)
        progress=client.get('/api/host/jobs/'+job['id'],headers=headers).json
        self.assertEqual(progress['status'],'running')
        self.assertIn('first',progress['output'])
        self.assertEqual(self.wait_job(client,job['id'],headers)['output'],'firstsecond')

    def test_orchestrator_proxies_to_independent_client(self):
        host=self.host();server=make_server('127.0.0.1',0,host,threaded=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            headers=self.login()
            record=self.client.post('/api/clients',headers=headers,json={'name':'Test host','url':f'http://127.0.0.1:{server.server_port}','token':'t'*48}).json
            base='/api/clients/'+record['id']+'/remote/'
            self.assertEqual(self.client.get(base+'metrics').status_code,200)
            action=self.client.post(base+'actions',headers=headers,json={'name':'Proxy','command':'printf through-proxy','timeout':5}).json
            job=self.client.post(base+'actions/'+action['id']+'/run',headers=headers,json={}).json
            machine_headers={'Authorization':'Bearer '+'t'*48}
            result=self.wait_job(host.test_client(),job['id'],machine_headers)
            self.assertEqual(result['output'],'through-proxy')
            self.assertEqual(self.client.get(base+'jobs/'+job['id']).json['status'],'succeeded')
            self.assertEqual(self.client.post(base+'login',headers=headers,json={}).status_code,404)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_job_restart_marks_interrupted_and_commands_cannot_read_orca_environment(self):
        host=self.host();store=host.extensions['store']
        store.put('jobs',dict(id='interrupted',status='running',output='partial',finished=None))
        restarted=self.host()
        self.assertEqual(restarted.extensions['store'].get('jobs','interrupted')['status'],'interrupted')
        client=restarted.test_client();headers=self.login(client)
        with patch.dict(os.environ, {'ORCA_TEST_SECRET':'never-print-me'}):
            action=client.post('/api/host/actions',headers=headers,json={'name':'Environment','command':'printf "%s" "$ORCA_TEST_SECRET"','timeout':5}).json
            job=client.post('/api/host/actions/'+action['id']+'/run',headers=headers,json={}).json
            self.assertNotIn('never-print-me',self.wait_job(client,job['id'],headers)['output'])

    def test_ssh_command_uses_explicit_identity_and_host_verification(self):
        key=Path(self.temp.name)/'key';key.write_text('test');known=Path(self.temp.name)/'hosts';known.write_text('test')
        with patch.dict(os.environ,dict(ORCA_EXECUTION_MODE='ssh',ORCA_SSH_TARGET='orca@localhost',ORCA_SSH_KEY=str(key),ORCA_SSH_KNOWN_HOSTS=str(known))):
            execution=Execution()
            command=execution.command(dict(command='printf hello',cwd='',timeout=5))
            self.assertFalse(command['shell'])
            self.assertIn('StrictHostKeyChecking=yes',command['args'])
            self.assertIn('BatchMode=yes',command['args'])
            # Run the exact Python payload locally to test quoting and host execution.
            result=__import__('subprocess').run(shlex.split(command['args'][-1]),capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout,'hello')


if __name__=='__main__':
    unittest.main()
