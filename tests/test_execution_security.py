"""Exercise execution boundaries with disposable processes and Docker metadata."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import psutil

from orca_app.docker_service import DockerService
from orca_app.docker_worker import recipe_for
from orca_app.execution import Execution
from orca_app.image_updates import Docker, run_bounded


class ExecutionSecurityTests(unittest.TestCase):
    def test_capture_bounds_stdout_and_stderr_and_stops_producer(self):
        for stream in ('stdout', 'stderr'):
            with self.subTest(stream=stream):
                command = [sys.executable, '-u', '-c',
                           f'import sys,time; sys.{stream}.write("x"*16384); time.sleep(30)']
                started = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, 'output limit'):
                    run_bounded(command, timeout=5, max_output=8192)
                self.assertLess(time.monotonic() - started, 5)

    def test_capture_keeps_separate_utf8_streams(self):
        result = run_bounded([sys.executable, '-c',
            'import sys; print("café"); print("error",file=sys.stderr)'], timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'café\n')
        self.assertEqual(result.stderr, 'error\n')

    @unittest.skipIf(os.name == 'nt', 'POSIX process-group cleanup')
    def test_timeout_stops_descendant_that_keeps_output_pipe_open(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / 'child.pid'
            child = 'import time; time.sleep(30)'
            script = ('import subprocess,sys; '
                      f'p=subprocess.Popen([sys.executable,"-c",{child!r}]); '
                      f'open({str(pid_file)!r},"w").write(str(p.pid))')
            with self.assertRaises(subprocess.TimeoutExpired):
                run_bounded([sys.executable, '-c', script], timeout=.5)
            identifier = int(pid_file.read_text())
            try:
                process = psutil.Process(identifier)
                self.assertEqual(process.status(), psutil.STATUS_ZOMBIE)
            except psutil.NoSuchProcess:
                pass

    def test_docker_cli_rejects_oversized_untrusted_response(self):
        docker = Docker(command=[sys.executable, '-u', '-c',
            'import sys,time; sys.stdout.write("x"*(17*1024*1024)); time.sleep(30)'])
        with self.assertRaisesRegex(RuntimeError, 'output limit'):
            docker.text('info')

    def test_bundled_worker_enforces_docker_output_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'fake-docker'
            executable.write_text('#!' + sys.executable + '\n'
                'import sys,time\nsys.stdout.write("x"*(17*1024*1024)); sys.stdout.flush(); time.sleep(30)\n')
            executable.chmod(0o700)
            execution = Execution()
            execution.docker_prefix = [str(executable)]
            with self.assertRaisesRegex(RuntimeError, 'output limit'):
                execution.docker_query('inventory', timeout=5)

    def test_lifecycle_passes_argument_vector_without_shell(self):
        execution = Execution()
        execution.docker_prefix = ['docker&malicious-command', '--context', 'a&b']
        jobs = Mock()
        service = DockerService(Mock(), execution, jobs)
        service.container = Mock(return_value=dict(state='running', managed=False,
                                                   self_update=False, name='test'))
        service.lifecycle('a' * 64, 'restart')
        action = jobs.start.call_args.args[0]
        command = execution.command(action)
        self.assertFalse(command['shell'])
        self.assertEqual(command['args'], execution.docker_prefix + ['container', 'restart', 'a' * 64])

    def test_remote_argv_is_literal_and_remote_credentials_are_filtered(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'should-not-exist'
            argument = '; touch ' + str(marker)
            execution = Execution()
            execution.mode = 'ssh'
            execution.python = sys.executable
            action = dict(command='unused shell command', cwd='', timeout=5,
                          argv=[sys.executable, '-c',
                          'import json,os,sys; print(json.dumps([sys.argv[1],os.getenv("ORCA_CLIENT_TOKEN"),os.getenv("VISIBLE")]))',
                          argument])
            with patch.object(execution, 'ssh', side_effect=lambda payload:
                    shlex.split(Execution.ssh(execution, payload)[-1])):
                command = execution.command(action)
            env = dict(os.environ, ORCA_CLIENT_TOKEN='private-token', VISIBLE='present')
            result = subprocess.run(**command, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), [argument, None, 'present'])
            self.assertFalse(marker.exists())

    def test_remote_metrics_do_not_inherit_orca_credentials(self):
        execution = Execution()
        execution.mode = 'ssh'
        result = subprocess.CompletedProcess([], 0, '{}', '')
        with patch.dict(os.environ, {'ORCA_CLIENT_TOKEN': 'private-token', 'VISIBLE': 'present'}):
            with patch('orca_app.execution.run_bounded', return_value=result) as run:
                execution.metrics()
        env = run.call_args.kwargs['env']
        self.assertNotIn('ORCA_CLIENT_TOKEN', env)
        self.assertEqual(env['VISIBLE'], 'present')

    def test_windows_metadata_does_not_generate_posix_shell_command(self):
        labels = {'com.docker.compose.project': 'example&whoami',
                  'com.docker.compose.project.working_dir': 'C:\\deploy',
                  'com.docker.compose.project.config_files': 'C:\\deploy\\compose.yaml'}
        with patch('orca_app.docker_worker.sys.platform', 'win32'):
            recipe = recipe_for(Docker(), labels)
        self.assertEqual(recipe['command'], '')
        self.assertIn('Windows update command', recipe['reason'])


if __name__ == '__main__':
    unittest.main()
