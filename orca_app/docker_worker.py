"""Read-only Docker queries. This source also runs on the SSH target's Python."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import time

from .image_updates import Docker, registry_image_id


def cache_key(endpoint, reference, platform):
    return hashlib.sha256(json.dumps([endpoint, reference, platform]).encode()).hexdigest()


def identity(endpoint, project, name):
    # Compose project names identify the deployment on this daemon, independent of file moves.
    return hashlib.sha256(json.dumps([endpoint, 'compose', project] if project else [endpoint, 'container', name]).encode()).hexdigest()


def recipe_for(docker, labels):
    project = labels.get('com.docker.compose.project', '')
    cwd = labels.get('com.docker.compose.project.working_dir', '')
    files = labels.get('com.docker.compose.project.config_files', '')
    env = labels.get('com.docker.compose.project.environment_file', '')
    reason = ''
    argv = docker.prefix + ['compose', '--project-name', project]
    paths = files.split(',') if files else []
    try:
        if not project:
            reason = 'Provide an update command for this container or its deployment manager.'
        elif not cwd or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            reason = 'The original Compose working directory is unavailable on the execution target.'
        elif not paths or any(not p or not Path(p).is_absolute() or not Path(p).is_file() for p in paths):
            reason = 'The original Compose files are unavailable on the execution target.'
        elif env and any(not p or not Path(p).is_absolute() or not Path(p).is_file() for p in env.split(',')):
            reason = 'The original environment files are unavailable on the execution target.'
    except OSError as error:
        # These checks run as the execution user, even when Docker itself uses sudo.
        # A protected deployment path must not prevent discovery of other containers.
        path = error.filename or cwd
        reason = f'The original Compose configuration cannot be accessed by the execution user: {path}. Provide an update command and an accessible working directory.'
    if labels.get('com.docker.swarm.service.name'):
        reason = 'Provide an update command for the Swarm service or stack.'
    argv += ['--project-directory', cwd]
    for path in paths:
        argv += ['--file', path]
    if env:
        for path in env.split(','):
            argv += ['--env-file', path]
    # Explicit profiles are needed to include existing services using optional profiles.
    # Compose labels cannot recover original profile selection: let the user review/edit.
    command = '' if reason else shlex.join(argv + ['pull']) + ' && ' + shlex.join(argv + ['up', '-d'])
    return dict(command=command, cwd=cwd, timeout=3600, reason=reason)


def inventory(docker, self_id='', stats=True, self_token_hash='', identifier=None):
    focused = bool(identifier)
    endpoint = docker.json('info', '--format', '{{json .ID}}')
    if not endpoint:
        raise RuntimeError('Docker daemon identity is unavailable.')
    selected = None
    if identifier:
        matches = docker.json('container', 'inspect', identifier)
        selected = next((c for c in matches if c['Id'] == identifier), None)
        if selected is None:
            return dict(containers=[], warnings=[], endpoint=endpoint)
        project = (selected.get('Config', {}).get('Labels') or {}).get('com.docker.compose.project')
        ids = docker.text('ps', '--all', '--quiet', '--no-trunc', '--filter',
                          'label=com.docker.compose.project=' + project).split() if project else [identifier]
        if identifier not in ids:
            ids.append(identifier)
        stats = False
    else:
        ids = docker.text('ps', '--all', '--quiet', '--no-trunc').split()
    containers = []
    warnings = []
    # Inspect in batches, retry individually if a container disappears during discovery.
    for offset in range(0, len(ids), 100):
        batch = ids[offset:offset + 100]
        try:
            containers.extend(docker.json('container', 'inspect', *batch))
        except RuntimeError:
            for identifier in batch:
                try:
                    containers.extend(docker.json('container', 'inspect', identifier))
                except RuntimeError as error:
                    warnings.append(f'Container {identifier[:12]}: {error}')
    measures = {}
    if stats and ids:
        try:
            for line in docker.text('stats', '--all', '--no-stream', '--no-trunc', '--format', '{{json .}}').splitlines():
                entry = json.loads(line)
                measures[entry['ID']] = entry
        except (RuntimeError, ValueError, KeyError) as error:
            warnings.append('Statistics unavailable: ' + str(error))
    images = {}
    rows = []
    own_groups = set()
    recipes = {}
    project_metadata = {}
    for container in containers:
        identifier = container['Id']
        config = container.get('Config') or {}
        labels = config.get('Labels') or {}
        status = container.get('State') or {}
        name = container.get('Name', identifier[:12]).lstrip('/')
        reference = config.get('Image', '')
        project = labels.get('com.docker.compose.project', '')
        group = identity(endpoint, project, name)
        metadata = tuple(labels.get('com.docker.compose.project.' + key, '')
                         for key in ('working_dir', 'config_files', 'environment_file'))
        if group not in recipes:
            recipes[group] = recipe_for(docker, labels)
            project_metadata[group] = metadata
        elif project_metadata[group] != metadata:
            recipes[group].update(command='', reason='Project containers disagree about their Compose configuration. Provide an update command.')
        platform = None
        platform_error = ''
        try:
            current = container['Image']
            if not focused and current not in images:
                images[current] = docker.json('image', 'inspect', current)[0]
            if not focused:
                image = images[current]
                platform = [image.get('Os'), image.get('Architecture'), image.get('Variant', ''), image.get('OsVersion', '')]
                if not all(platform[:2]):
                    raise RuntimeError('Image platform is unavailable.')
        except (RuntimeError, KeyError) as error:
            platform = None
            platform_error = str(error)
        tag = ('sha256:' + reference.split('@sha256:', 1)[1][:12] if '@sha256:' in reference else
               reference.rsplit(':', 1)[1] if reference.rfind(':') > reference.rfind('/') else 'latest')
        if re.fullmatch(r'(sha256:)?[0-9a-f]{12,64}', reference):
            tag = 'image-id'
        measure = measures.get(identifier) or {}
        live = status.get('Status') == 'running'
        row = dict(id=identifier, name=name, tag=tag, image=reference, image_id=container.get('Image', ''),
                   project=project, group=group, state=status.get('Status', 'unknown'),
                   health=(status.get('Health') or {}).get('Status'), exit_code=status.get('ExitCode'),
                   oom=bool(status.get('OOMKilled')), cpu=measure.get('CPUPerc') if live else None,
                   memory=(measure.get('MemUsage', '').split(' / ')[0] or None) if live else None,
                   platform=platform, platform_error=platform_error,
                   cache_key=cache_key(endpoint, reference, platform) if platform else None,
                   generated=recipes[group], managed=bool(labels.get('com.docker.swarm.service.name')))
        rows.append(row)
        own_token = any(value.startswith('ORCA_CLIENT_TOKEN=') and
                        hashlib.sha256(value.split('=', 1)[1].encode()).hexdigest() == self_token_hash
                        for value in (config.get('Env') or [])) if self_token_hash else False
        if own_token or (self_id and identifier.startswith(self_id)):
            own_groups.add(group)
    for row in rows:
        row['self_update'] = row['group'] in own_groups
    return dict(containers=sorted(rows, key=lambda row: row['name'].lower()), warnings=warnings, endpoint=endpoint)


def check_images(docker, self_id=''):
    data = inventory(docker, self_id, stats=False)
    results = {}
    for row in data['containers']:
        key = row['cache_key']
        reference = row['image']
        if key in results:
            continue
        checked_at = time.time()
        record = dict(id=key or row['id'], image=reference, platform=row['platform'], checked_at=checked_at)
        if not key:
            record.update(error=row['platform_error'] or 'Image platform unavailable.')
        elif '@' in reference:
            record.update(pinned=True)
        elif not reference or re.fullmatch(r'(sha256:)?[0-9a-f]{12,64}', reference):
            record.update(error='No registry repository/tag is configured for this image.')
        else:
            try:
                record['target'] = registry_image_id(docker, reference, row['platform'])
            except RuntimeError as error:
                record['error'] = str(error)
        results[record['id']] = record
    return dict(**data, results=list(results.values()))


def worker_main(payload):
    docker = Docker(payload.get('context'), payload.get('command'))
    try:
        if payload['operation'] == 'inventory':
            result = inventory(docker, payload.get('self_id', ''), payload.get('stats', True), payload.get('self_token_hash', ''), payload.get('identifier'))
        elif payload['operation'] == 'check':
            result = check_images(docker, payload.get('self_id', ''))
        else:
            raise RuntimeError('Unknown Docker query.')
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps(dict(error=str(error))))
