#!/usr/bin/env python3
"""Configure an Ubuntu/Debian host for Orca's Docker-to-host SSH mode."""

import argparse
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile


ACCOUNT = 'orca-run'
SSH_DIR = Path('/etc/orca-ssh')
KEY = SSH_DIR / 'id_ed25519'


def run(*args, capture=False):
    print('+ ' + ' '.join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, text=True,
                          stdout=subprocess.PIPE if capture else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container-uid', type=int, default=10001)
    parser.add_argument('--container-gid', type=int, default=10001)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run this script with sudo python3 scripts/setup_host.py')
    if args.container_uid < 1 or args.container_gid < 1:
        parser.error('Container UID and GID must be positive.')
    if not shutil.which('apt-get') or not shutil.which('systemctl'):
        parser.error('This script requires Ubuntu/Debian with systemd.')

    print('This grants orca-run unrestricted passwordless sudo (root access).', flush=True)
    run('apt-get', 'update')
    run('apt-get', 'install', '-y', 'openssh-server', 'python3', 'python3-venv', 'sudo')
    run('systemctl', 'enable', '--now', 'ssh')

    try:
        account = pwd.getpwnam(ACCOUNT)
    except KeyError:
        run('useradd', '--create-home', '--shell', '/bin/bash', ACCOUNT)
        account = pwd.getpwnam(ACCOUNT)
    if account.pw_uid == 0 or account.pw_shell.endswith(('nologin', '/false')):
        raise RuntimeError('Existing orca-run account must be non-root with a login shell.')
    home = Path(account.pw_dir)
    if not home.is_dir() or home == Path('/'):
        raise RuntimeError('orca-run must have an existing dedicated home directory.')
    venv = home / '.orca-host'
    run('runuser', '-u', ACCOUNT, '--', 'python3', '-m', 'venv', venv)
    run('runuser', '-u', ACCOUNT, '--', venv / 'bin/python', '-m', 'pip',
        'install', 'psutil==7.2.2')

    SSH_DIR.mkdir(mode=0o755, exist_ok=True)
    os.chown(SSH_DIR, 0, 0)
    SSH_DIR.chmod(0o755)
    if not KEY.exists():
        run('ssh-keygen', '-t', 'ed25519', '-N', '', '-f', KEY)
    # Derive the public key from the existing private key, also on repeat runs.
    public_key = run('ssh-keygen', '-y', '-P', '', '-f', KEY, capture=True).stdout.strip()
    public_file = Path(str(KEY) + '.pub')
    public_file.write_text(public_key + '\n')
    os.chown(public_file, 0, 0)
    public_file.chmod(0o644)
    os.chown(KEY, args.container_uid, args.container_gid)
    KEY.chmod(0o600)

    user_ssh = home / '.ssh'
    user_ssh.mkdir(mode=0o700, exist_ok=True)
    os.chown(user_ssh, account.pw_uid, account.pw_gid)
    user_ssh.chmod(0o700)
    authorized = user_ssh / 'authorized_keys'
    existing = authorized.read_text() if authorized.exists() else ''
    key_parts = public_key.split()[:2]
    if not any(line.split()[:2] == key_parts for line in existing.splitlines()):
        with authorized.open('a') as stream:
            stream.write(('\n' if existing and not existing.endswith('\n') else '') + public_key + '\n')
    os.chown(authorized, account.pw_uid, account.pw_gid)
    authorized.chmod(0o600)

    # Trust the host's key directly from its local public key file.
    host_key = Path('/etc/ssh/ssh_host_ed25519_key.pub')
    if not host_key.exists():
        run('ssh-keygen', '-A')
    known_hosts = SSH_DIR / 'known_hosts'
    known_hosts.write_text('host.docker.internal ' + host_key.read_text().strip() + '\n')
    os.chown(known_hosts, 0, 0)
    known_hosts.chmod(0o644)

    # Validate before installing; use an atomic replacement in sudoers.d.
    sudoers_dir = Path('/etc/sudoers.d')
    sudoers_dir.mkdir(mode=0o750, exist_ok=True)
    staged = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', prefix='.orca-run-',
                                         dir=sudoers_dir, delete=False) as stream:
            staged = Path(stream.name)
            stream.write('orca-run ALL=(ALL:ALL) NOPASSWD: ALL\n')
        staged.chmod(0o440)
        os.chown(staged, 0, 0)
        run('visudo', '-c', '-f', staged)
        staged.replace(sudoers_dir / 'orca-run')
    finally:
        if staged is not None and staged.exists():
            staged.unlink()
    run('visudo', '-c')
    # Verify the rule without relying on cached sudo credentials.
    result = run('runuser', '-u', ACCOUNT, '--', 'sudo', '-n', '-k',
                 '/usr/bin/id', '-u', capture=True)
    if result.stdout.strip() != '0':
        raise RuntimeError('Passwordless sudo verification failed.')

    print('\nHost setup complete. Add these to your existing client .env:\n')
    print(f'''ORCA_EXECUTION_MODE=ssh
ORCA_SSH_TARGET=orca-run@host.docker.internal
ORCA_SSH_PORT=22
ORCA_SSH_PYTHON={venv}/bin/python
ORCA_SSH_KEY_FILE={KEY}
ORCA_SSH_KNOWN_HOSTS_FILE={known_hosts}''')
    print('\nThen restart the client:\n'
          'sudo docker compose -f compose.client.yaml -f compose.client-ssh.yaml up -d --no-build')
    print('\nIn Orca, test: whoami; hostname; sudo -n id')


if __name__ == '__main__':
    # Avoid depending on access to the caller's working directory as orca-run.
    os.chdir('/')
    try:
        main()
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print(f'\nSetup stopped: {error}\nFix the issue and rerun the script.', file=sys.stderr)
        sys.exit(1)
