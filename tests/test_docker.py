"""Docker behavior with disposable metadata and commands; never changes real containers."""
import copy
import json
import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from orca_app.app import create_app
from orca_app.docker_worker import inventory, check_images, recipe_for
from orca_app.execution import Execution


def digest(c):
    return 'sha256:' + c * 64


class FakeDocker:
    prefix = ['docker', '--context', 'test']

    def __init__(self, folder):
        self.calls = []
        self.targets = {'web:latest': digest('b'), 'redis:7': digest('c')}
        self.rows = []
        for i, name, image, current, status in [('a','web','web:latest',digest('a'),'running'),
                                             ('b','redis','redis:7',digest('c'),'exited')]:
            self.rows.append(dict(Id=i*64, Name='/'+name, Image=current,
                State=dict(Status=status, Running=status=='running', ExitCode=0, Health=dict(Status='healthy')),
                Config=dict(Image=image, Labels={
                    'com.docker.compose.project':'media', 'com.docker.compose.service':name,
                    'com.docker.compose.project.working_dir':str(folder),
                    'com.docker.compose.project.config_files':str(folder/'custom file.yaml')+','+str(folder/'override.yaml'),
                    'com.docker.compose.project.environment_file':str(folder/'production.env')})))

    def text(self, *args):
        self.calls.append(args)
        if args[:1] == ('ps',):
            assert '--all' in args
            return '\n'.join(row['Id'] for row in self.rows)
        if args[0] == 'stats':
            return '\n'.join(json.dumps(dict(ID=row['Id'], CPUPerc='1.25%', MemUsage='12MiB / 8GiB')) for row in self.rows)
        raise AssertionError(args)

    def json(self, *args):
        self.calls.append(args)
        if args[0] == 'info': return 'daemon-id'
        if args[:2] == ('container','inspect'):
            return [copy.deepcopy(row) for row in self.rows if row['Id'] in args[2:]]
        if args[:2] == ('image','inspect'):
            return [dict(Os='linux', Architecture='amd64')]
        if args[:3] == ('buildx','imagetools','inspect'):
            if args[3] == '--format': return dict(os='linux',architecture='amd64')
            target=self.targets[args[-1]]
            if isinstance(target, Exception): raise target
            return dict(config=dict(digest=target))
        raise AssertionError(args)


class DockerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name)
        for name in ['custom file.yaml','override.yaml','production.env']:
            (self.folder/name).write_text('')
        self.fake=FakeDocker(self.folder)
        self.env=patch.dict(os.environ,{'ORCA_EXECUTION_MODE':'local','ORCA_DOCKER_COMMAND':'docker','ORCA_DOCKER_CONTEXT':''})
        self.env.start()
        self.app=create_app(dict(TESTING=True,ROLE='client',ADMIN_PASSWORD='test-password-for-orca',
            SECRET_KEY='s'*48,CLIENT_TOKEN='t'*48,DATABASE=str(self.folder/'client.db')))
        self.service=self.app.extensions['docker'];self.store=self.app.extensions['store']
        self.client=self.app.test_client();self.headers={'Authorization':'Bearer '+'t'*48}
        self.query=patch.object(self.service.execution,'docker_query',side_effect=lambda op,**kw:
            check_images(self.fake) if op=='check' else inventory(self.fake))
        self.query.start()
        self.base='/api/host/docker/'+'a'*64

    def tearDown(self):
        self.query.stop();self.env.stop();self.temp.cleanup()

    def wait_job(self,job):
        for _ in range(200):
            result=self.store.get('jobs',job['id'])
            if result['status']!='running' and not self.app.extensions['jobs'].active:return result
            time.sleep(.01)
        self.fail('Job did not finish')

    def test_discovery_generates_project_command_preserving_paths_and_stats(self):
        data=inventory(self.fake)
        redis,web=data['containers']
        self.assertEqual(redis['group'],web['group'])
        self.assertIsNone(redis['cpu']);self.assertIsNone(redis['memory'])
        self.assertEqual(web['cpu'],'1.25%');self.assertEqual(web['memory'],'12MiB')
        command=web['generated']['command'];argv=shlex.split(command)
        self.assertEqual(argv[:4],['docker','--context','test','compose'])
        self.assertIn(str(self.folder/'custom file.yaml'),argv)
        self.assertLess(argv.index(str(self.folder/'custom file.yaml')),argv.index(str(self.folder/'override.yaml')))
        self.assertIn(str(self.folder/'production.env'),argv)
        self.assertIn('pull &&',command);self.assertTrue(command.endswith('up -d'))
        self.assertNotIn(' up -d web',command)
        self.assertEqual(inventory(self.fake,self_id='a'*12)['containers'][0]['self_update'],True)

    def test_project_identity_survives_file_moves_and_conflicting_labels_disable_generation(self):
        original=inventory(self.fake)['containers'][0]['group']
        self.fake.rows[0]['Config']['Labels']['com.docker.compose.project.config_files']=str(self.folder/'override.yaml')
        rows=inventory(self.fake)['containers']
        self.assertTrue(all(row['group']==original for row in rows))
        self.assertTrue(all(not row['generated']['command'] for row in rows))
        self.fake.rows[0]['Config']['Env']=['ORCA_CLIENT_TOKEN=unique-client-token']
        data=inventory(self.fake,self_token_hash=hashlib.sha256(b'unique-client-token').hexdigest())
        self.assertTrue(all(row['self_update'] for row in data['containers']))

    def test_missing_files_and_manager_metadata_require_manual_command(self):
        labels=self.fake.rows[0]['Config']['Labels']
        (self.folder/'override.yaml').unlink()
        self.assertEqual(recipe_for(self.fake,labels)['command'],'')
        self.assertEqual(recipe_for(self.fake,{'com.docker.swarm.service.name':'stack_web'})['command'],'')
        self.assertEqual(recipe_for(self.fake,{})['command'],'')

    def test_manual_recipe_shared_and_survives_recreation_and_confirmation_change(self):
        self.client.get('/api/host/docker',headers=self.headers)
        recipe=dict(command='printf updated',cwd=str(self.folder),timeout=5)
        self.assertEqual(self.client.put(self.base+'/recipe',headers=self.headers,json=recipe).status_code,200)
        rows=self.client.get('/api/host/docker',headers=self.headers).json['containers']
        self.assertTrue(all(row['recipe']['edited'] for row in rows))
        confirmation=self.client.post(self.base+'/prepare',headers=self.headers,json={}).json
        self.client.put(self.base+'/recipe',headers=self.headers,json=dict(recipe,command='printf changed'))
        rejected=self.client.post(self.base+'/update',headers=self.headers,json={'confirmation':confirmation['id']})
        self.assertEqual(rejected.status_code,400)
        self.assertIn('changed',rejected.json['error'])
        self.fake.rows[0]['Id']='d'*64;self.service.invalidate()
        row=next(row for row in self.service.listing()['containers'] if row['name']=='web')
        self.assertEqual(row['recipe']['command'],'printf changed')
        self.assertEqual(self.client.post(self.base+'/prepare',headers=self.headers,json={}).status_code,400)

    def test_check_is_builtin_persisted_per_image_and_recompared_after_recreation(self):
        self.assertEqual(self.client.get('/api/host/orca-actions',headers=self.headers).json[0]['id'],'orca-check-updates')
        self.assertEqual(self.client.delete('/api/host/actions/orca-check-updates',headers=self.headers).status_code,404)
        job=self.client.post('/api/host/orca-actions/orca-check-updates/run',headers=self.headers,json={}).json
        result=self.wait_job(job)
        self.assertEqual(result['status'],'succeeded');self.assertIn('web (web:latest): UPDATE AVAILABLE',result['output'])
        rows=self.service.listing()['containers']
        self.assertEqual([row['update_available'] for row in rows],[False,True])
        self.assertEqual(len(self.store.all('docker-images')),2)
        self.fake.rows[0]['Image']=digest('b');self.fake.rows[0]['Id']='d'*64;self.service.invalidate()
        self.assertFalse(any(row['update_available'] for row in self.service.listing()['containers']))
        self.fake.targets['web:latest']=RuntimeError('Registry authentication failed')
        result=self.wait_job(self.service.check())
        self.assertEqual(result['status'],'failed');self.assertIn('CHECK FAILED',result['output'])
        record=next(item for item in self.store.all('docker-images') if item['image']=='web:latest')
        self.assertEqual(record['target'],digest('b'));self.assertIn('last_success',record)
        self.fake.rows[0]['Config']['Image']='web:stable';self.service.invalidate()
        self.assertFalse(any(row['update_available'] for row in self.service.listing()['containers']))

    def test_update_confirmation_runs_exact_command_and_all_actions_share_one_job(self):
        recipe=dict(command='printf first; sleep .3; printf last',cwd=str(self.folder),timeout=5)
        self.client.put(self.base+'/recipe',headers=self.headers,json=recipe)
        confirmation=self.client.post(self.base+'/prepare',headers=self.headers,json={}).json
        result=self.client.post(self.base+'/update',headers=self.headers,json={'confirmation':confirmation['id']})
        self.assertEqual(result.status_code,202)
        self.assertEqual(self.client.post('/api/host/orca-actions/orca-check-updates/run',headers=self.headers,json={}).status_code,409)
        self.assertEqual(self.client.put(self.base+'/recipe',headers=self.headers,json=recipe).status_code,409)
        # Metadata reads remain available while a job runs.
        self.assertEqual(self.client.get('/api/host/docker',headers=self.headers).status_code,200)
        job=self.wait_job(result.json)
        self.assertEqual(job['output'],'firstlast');self.assertEqual(job['command'],recipe['command'])
        self.assertEqual(self.client.post(self.base+'/update',headers=self.headers,json={'confirmation':confirmation['id']}).status_code,400)

    def test_lifecycle_rechecks_state_and_uses_fixed_container_id(self):
        with patch.object(self.service.jobs,'start',return_value={'id':'job'}) as start:
            self.assertEqual(self.client.post(self.base+'/control',headers=self.headers,json={'operation':'pause'}).status_code,202)
            self.assertEqual(shlex.split(start.call_args.args[0]['command']),['docker','container','pause','a'*64])
            self.fake.rows[0]['State']['Status']='paused'
            self.assertEqual(self.client.post(self.base+'/control',headers=self.headers,json={'operation':'stop'}).status_code,400)
            self.assertEqual(self.client.post(self.base+'/control',headers=self.headers,json={'operation':'unpause'}).status_code,202)
            self.assertEqual(self.client.post(self.base+'/control',headers=self.headers,json={'operation':'stop; evil'}).status_code,400)
        self.assertEqual(self.client.get('/api/host/docker').status_code,401)
        self.assertEqual(self.client.put(self.base+'/recipe',json={}).status_code,401)

    def test_worker_payload_runs_locally_and_over_ssh_without_installed_orca(self):
        # Test actual bundled Python execution using a disposable Docker CLI, not a daemon.
        executable=self.folder/'fake-docker'
        executable.write_text('#!'+sys.executable+'\nimport json,sys\nprint(json.dumps("daemon") if sys.argv[1]=="info" else "")\n')
        executable.chmod(0o700)
        execution=Execution();execution.docker_prefix=[str(executable)]
        result=execution.docker_query('inventory')
        self.assertEqual(result['containers'],[])
        execution.mode='ssh';execution.python=sys.executable
        # Execute the exact SSH remote Python command locally.
        with patch.object(execution,'ssh',side_effect=lambda payload:shlex.split(Execution.ssh(execution,payload)[-1])):
            result=execution.docker_query('inventory')
        self.assertEqual(result['containers'],[])


if __name__=='__main__':unittest.main()
