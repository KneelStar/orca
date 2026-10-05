#!/usr/bin/env python3
"""Check images of Compose apps without pulling or changing containers.

Requires Python 3.10+, Docker CLI access, and the Docker Buildx plugin. Registry
credentials come from the invoking user's Docker configuration. This checks
existing containers on one Docker endpoint, not every service defined in a stack
or changes to its deployment files. Swarm tasks need manager-level discovery.
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict


MAX_DOCKER_OUTPUT = 16 * 1024 * 1024


def terminate_process(process):
    """Stop a process and its children; callers create a new process session."""
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def run_bounded(args, timeout, max_output=MAX_DOCKER_OUTPUT, env=None, stdin=None):
    """Capture CLI output without trusting the size of daemon/registry responses."""
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.DEVNULL if stdin is None else stdin, env=env,
                               start_new_session=os.name != 'nt')
    buffers = [bytearray(), bytearray()]
    exceeded = threading.Event()
    lock = threading.Lock()
    total = 0
    deadline = time.monotonic() + timeout

    def read(stream, buffer):
        nonlocal total
        try:
            while chunk := stream.read1(65536):
                with lock:
                    if total + len(chunk) > max_output:
                        exceeded.set()
                    else:
                        total += len(chunk)
                        buffer.extend(chunk)
                if exceeded.is_set():
                    terminate_process(process)
                    break
        finally:
            stream.close()

    readers = [threading.Thread(target=read, args=(stream, buffer), daemon=True)
               for stream, buffer in zip((process.stdout, process.stderr), buffers)]
    for reader in readers:
        reader.start()
    try:
        process.wait(timeout=timeout)
        # A child may keep the pipes open after the CLI itself exits.
        for reader in readers:
            reader.join(max(0, deadline - time.monotonic()))
        if any(reader.is_alive() for reader in readers):
            raise subprocess.TimeoutExpired(args, timeout)
        if exceeded.is_set():
            raise RuntimeError('Command response exceeded its output limit.')
        return subprocess.CompletedProcess(args, process.returncode,
            *(bytes(buffer).decode('utf-8', errors='replace') for buffer in buffers))
    finally:
        if process.poll() is None or any(reader.is_alive() for reader in readers):
            terminate_process(process)
        process.wait()
        for reader in readers:
            reader.join(1)


class Docker:
    def __init__(self, context=None, command=None):
        self.prefix = (command or ['docker']) + (['--context', context] if context else [])

    def text(self, *args):
        try:
            result = run_bounded(self.prefix + list(args), timeout=30)
        except FileNotFoundError as error:
            raise RuntimeError('Docker CLI is not installed.') from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError('Docker or the registry did not respond within 30 seconds.') from error
        if result.returncode:
            detail = result.stderr.strip().splitlines()
            raise RuntimeError(detail[-1][:300] if detail else 'Docker command failed.')
        return result.stdout

    def json(self, *args):
        try:
            return json.loads(self.text(*args))
        except ValueError as error:
            raise RuntimeError('Docker returned invalid JSON.') from error


def repository(reference):
    name = reference.split('@', 1)[0]
    if name.rfind(':') > name.rfind('/'):
        name = name[:name.rfind(':')]
    return name


def registry_image_id(docker, reference, platform):
    """Resolve a tag to its matching platform's image configuration digest."""
    manifest = docker.json('buildx', 'imagetools', 'inspect', '--raw', reference)
    matched_platform = False
    for _ in range(4):
        if 'manifests' not in manifest:
            digest = manifest.get('config', {}).get('digest', '')
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
                raise RuntimeError('Registry manifest has no supported image configuration digest.')
            if not matched_platform:
                config = docker.json('buildx', 'imagetools', 'inspect', '--format',
                                     '{{json .Image}}', reference)
                if config.get('os') != platform[0] or config.get('architecture') != platform[1]:
                    raise RuntimeError('Registry image does not match the installed image platform.')
                if platform[2] and config.get('variant', '') != platform[2]:
                    raise RuntimeError('Registry image CPU variant does not match.')
                if platform[3] and config.get('os.version', '') != platform[3]:
                    raise RuntimeError('Registry image OS version does not match.')
            return digest
        matches = []
        for entry in manifest['manifests']:
            target = entry.get('platform', {})
            if target.get('os') != platform[0] or target.get('architecture') != platform[1]:
                continue
            if platform[2] and target.get('variant', '') != platform[2]:
                continue
            if platform[3] and target.get('os.version', '') != platform[3]:
                continue
            matches.append(entry)
        if len(matches) != 1:
            raise RuntimeError('Registry platform match is missing or ambiguous.')
        matched_platform = True
        digest = matches[0].get('digest', '')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise RuntimeError('Registry manifest has an invalid platform digest.')
        manifest = docker.json('buildx', 'imagetools', 'inspect', '--raw',
                               repository(reference) + '@' + digest)
    raise RuntimeError('Registry manifest nesting is unsupported.')


def app_identity(container):
    labels = container.get('Config', {}).get('Labels') or {}
    swarm = labels.get('com.docker.swarm.service.name')
    if swarm:
        # The result covers local tasks only, not every service in the Swarm stack.
        return ('swarm-service', swarm)
    project = labels.get('com.docker.compose.project')
    if project:
        return ('compose', project)
    return ('container', container['Name'].lstrip('/'))


def check_apps(docker, app=None):
    groups = defaultdict(list)
    warnings = []
    ids = docker.text('ps', '--all', '--quiet').split()
    for identifier in ids:
        try:
            container = docker.json('container', 'inspect', identifier)[0]
        except RuntimeError as error:
            warnings.append(f'Could not inspect container {identifier}: {error}')
            continue
        key = app_identity(container)
        if key and (app is None or key[1] == app):
            groups[key].append(container)

    images = {}
    registry = {}
    results = []
    for (kind, name), containers in sorted(groups.items()):
        records = []
        for container in containers:
            config = container['Config']
            reference = config.get('Image', '')
            labels = config.get('Labels') or {}
            record = dict(container=container['Name'].lstrip('/'),
                          service=labels.get('com.docker.compose.service') or name,
                          image=reference)
            if '@' in reference:
                record.update(status='pinned', detail='Digest-pinned image needs an explicit tag or release policy to check.')
            elif not reference or re.fullmatch(r'(sha256:)?[0-9a-f]{12,64}', reference):
                record.update(status='unknown', detail='Image has no registry repository/tag to check.')
            else:
                try:
                    current = container['Image']
                    if current not in images:
                        images[current] = docker.json('image', 'inspect', current)[0]
                    image = images[current]
                    platform = (image.get('Os'), image.get('Architecture'),
                                image.get('Variant', ''), image.get('OsVersion', ''))
                    if not all(platform[:2]):
                        raise RuntimeError('Installed image platform is unavailable.')
                    key = (reference, platform)
                    if key not in registry:
                        try:
                            registry[key] = registry_image_id(docker, reference, platform)
                        except RuntimeError as error:
                            registry[key] = error
                    target = registry[key]
                    if isinstance(target, RuntimeError):
                        raise target
                    record.update(status='update' if current != target else 'current')
                except RuntimeError as error:
                    record.update(status='unknown', detail=str(error))
            records.append(record)
        statuses = {record['status'] for record in records}
        status = ('update' if 'update' in statuses else
                  'unknown' if 'unknown' in statuses else
                  'pinned' if statuses == {'pinned'} else
                  'partial' if 'pinned' in statuses else 'current')
        results.append(dict(kind=kind, name=name, status=status, containers=records))
    return dict(scope='Existing containers on the selected Docker endpoint only.',
                apps=results, warnings=warnings)
