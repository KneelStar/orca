"""Local execution, or explicit SSH access to a POSIX host from a container."""
import base64
import json
import os
import platform
import re
import shlex
import subprocess
import signal
import sys
from pathlib import Path

import psutil

from .image_updates import run_bounded


# Executed by the host's Python, with its own timeout if the SSH connection drops.
REMOTE_SCRIPT = '''
import base64,json,os,platform,signal,subprocess,sys
payload=json.loads(base64.b64decode(sys.argv[1]))
if payload['kind']=='metrics':
 import psutil
 disks=[]
 for part in psutil.disk_partitions():
  if part.fstype in ('squashfs','iso9660'): continue
  try:
   usage=psutil.disk_usage(part.mountpoint)
   disks.append(dict(mount=part.mountpoint,percent=usage.percent,total=usage.total,used=usage.used))
  except OSError: pass
 print(json.dumps(dict(os=platform.system(),cpu=psutil.cpu_percent(interval=.1),ram=psutil.virtual_memory().percent,disks=disks)))
else:
 env={k:v for k,v in os.environ.items() if not k.upper().startswith('ORCA_')}
 argv=payload.get('argv')
 p=subprocess.Popen(argv if argv is not None else payload['command'],shell=argv is None,cwd=payload.get('cwd') or None,start_new_session=True,stdin=subprocess.DEVNULL,env=env)
 try: sys.exit(p.wait(timeout=payload['timeout']))
 except subprocess.TimeoutExpired:
  os.killpg(p.pid,signal.SIGKILL)
  p.wait()
  print('Action exceeded its time limit.',flush=True)
  sys.exit(124)
'''


class Execution:
    def __init__(self):
        self.docker_prefix = shlex.split(os.getenv('ORCA_DOCKER_COMMAND', 'docker'))
        if not self.docker_prefix:
            raise ValueError('ORCA_DOCKER_COMMAND cannot be empty.')
        self.docker_context = os.getenv('ORCA_DOCKER_CONTEXT', '')
        if self.docker_context:
            self.docker_prefix += ['--context', self.docker_context]
        self.mode = os.getenv('ORCA_EXECUTION_MODE', 'local')
        if self.mode not in ('local', 'ssh'):
            raise ValueError('ORCA_EXECUTION_MODE must be local or ssh.')
        self.in_container = Path('/.dockerenv').exists() or os.getenv('ORCA_CONTAINER') == 'true'
        self.target = os.getenv('ORCA_SSH_TARGET', '')
        self.port = os.getenv('ORCA_SSH_PORT', '22')
        self.key = os.getenv('ORCA_SSH_KEY', '/run/orca/ssh_key')
        self.known_hosts = os.getenv('ORCA_SSH_KNOWN_HOSTS', '/run/orca/known_hosts')
        self.python = os.getenv('ORCA_SSH_PYTHON', 'python3')
        if self.mode == 'ssh':
            if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.:-]*', self.target):
                raise ValueError('ORCA_SSH_TARGET must be user@hostname or user@IP.')
            if not self.port.isdigit() or not 1 <= int(self.port) <= 65535:
                raise ValueError('Invalid ORCA_SSH_PORT.')
            for path in (self.key, self.known_hosts):
                if not Path(path).is_file():
                    raise ValueError('SSH mode requires a mounted key and verified known_hosts file.')

    @property
    def label(self):
        return 'Host via SSH' if self.mode == 'ssh' else ('Container' if self.in_container else 'Host')

    def ssh(self, payload):
        code = base64.b64encode(REMOTE_SCRIPT.encode()).decode()
        arg = base64.b64encode(json.dumps(payload).encode()).decode()
        loader = "import base64;exec(base64.b64decode('" + code + "'))"
        command = shlex.join([self.python, '-u', '-c', loader, arg])
        return ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
                '-o', 'IdentitiesOnly=yes', '-o', 'UserKnownHostsFile=' + self.known_hosts,
                '-i', self.key, '-p', self.port, self.target, command]

    def command(self, action):
        if self.mode == 'ssh':
            # Job metadata has its own kind; only execution fields belong in the SSH payload.
            payload = dict(kind='action', command=action['command'],
                           cwd=action.get('cwd') or '', timeout=action['timeout'])
            if 'argv' in action:
                payload['argv'] = action['argv']
            return dict(args=self.ssh(payload), shell=False, cwd=None)
        if 'argv' in action:
            return dict(args=action['argv'], shell=False, cwd=action.get('cwd') or None)
        return dict(args=action['command'], shell=True, cwd=action.get('cwd') or None)

    def metrics(self):
        if self.mode == 'ssh':
            env = {key: value for key, value in os.environ.items() if not key.upper().startswith('ORCA_')}
            result = run_bounded(self.ssh({'kind': 'metrics'}), timeout=10, max_output=1024 * 1024, env=env)
            if result.returncode:
                raise RuntimeError('Host metrics unavailable. Check SSH access and Python/psutil on the host.')
            data = json.loads(result.stdout)
        else:
            disks = []
            for part in psutil.disk_partitions():
                if part.fstype in ('squashfs', 'iso9660'):
                    continue
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    disks.append(dict(mount=part.mountpoint, percent=usage.percent, total=usage.total, used=usage.used))
                except OSError:
                    pass
            data = dict(os=platform.system(), cpu=psutil.cpu_percent(interval=.1), ram=psutil.virtual_memory().percent, disks=disks)
        data['execution_target'] = self.label
        data['metrics_scope'] = 'Container-visible system metrics' if self.in_container and self.mode == 'local' else 'Host metrics'
        return data

    def docker_query(self, operation, timeout=45, identifier=None):
        # Bundle the read-only worker so SSH hosts need Python and Docker, not Orca installed.
        directory = Path(__file__).parent
        source = (directory / 'image_updates.py').read_text() + '\n' + (directory / 'docker_worker.py').read_text().replace(
            'from .image_updates import Docker, registry_image_id, installed_image_id', '')
        payload = dict(operation=operation, command=self.docker_prefix, identifier=identifier,
                       self_id=os.getenv('HOSTNAME', '') if self.in_container else '',
                       self_token_hash=getattr(self, 'self_token_hash', ''))
        source += '\nworker_main(' + repr(payload) + ')\n'
        loader = "import base64;exec(base64.b64decode(" + repr(base64.b64encode(source.encode()).decode()) + "))"
        if self.mode == 'ssh':
            command = self.ssh(dict(kind='action', command=shlex.join([self.python, '-u', '-c', loader]),
                                    cwd='', timeout=timeout))
        else:
            command = [sys.executable, '-u', '-c', loader]
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith('ORCA_')}
        try:
            process = run_bounded(command, timeout=timeout + (15 if self.mode == 'ssh' else 0),
                                  max_output=32 * 1024 * 1024, env=env)
        except subprocess.TimeoutExpired:
            raise RuntimeError('Docker query exceeded its time limit.')
        if process.returncode:
            raise RuntimeError(process.stderr.strip()[-500:] or 'Docker query failed on the execution target.')
        try:
            result = json.loads(process.stdout)
        except ValueError:
            raise RuntimeError('Docker query returned invalid data.')
        if result.get('error'):
            raise RuntimeError(result['error'])
        return result
