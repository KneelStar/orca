"""Local execution, or explicit SSH access to a POSIX host from a container."""
import base64
import json
import os
import platform
import re
import shlex
import subprocess
from pathlib import Path

import psutil


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
 p=subprocess.Popen(payload['command'],shell=True,cwd=payload.get('cwd') or None,start_new_session=True,stdin=subprocess.DEVNULL)
 try: sys.exit(p.wait(timeout=payload['timeout']))
 except subprocess.TimeoutExpired:
  os.killpg(p.pid,signal.SIGKILL)
  p.wait()
  print('Action exceeded its time limit.',flush=True)
  sys.exit(124)
'''


class Execution:
    def __init__(self):
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
            return dict(args=self.ssh(dict(kind='action', **action)), shell=False, cwd=None)
        return dict(args=action['command'], shell=True, cwd=action.get('cwd') or None)

    def metrics(self):
        if self.mode == 'ssh':
            result = subprocess.run(self.ssh({'kind': 'metrics'}), capture_output=True, text=True, timeout=10)
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
