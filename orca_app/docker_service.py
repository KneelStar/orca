"""Client-owned Docker recipes, registry cache, and job definitions."""
import shlex
import threading
import time
import uuid

from .jobs import Busy


BUILTIN = dict(id='orca-check-updates', name='Check image updates', builtin=True)
BUILTINS = (BUILTIN,)
LIFECYCLE = {'running': {'stop', 'pause', 'restart'}, 'paused': {'unpause'},
             'created': {'start'}, 'exited': {'start'}, 'restarting': {'stop'}}


class DockerService:
    def __init__(self, store, execution, jobs):
        self.store, self.execution, self.jobs = store, execution, jobs
        self.lock = threading.RLock()
        self.cached = None
        self.cached_at = 0

    def invalidate(self):
        with self.lock:
            self.cached_at = 0

    def listing(self, fresh=False, identifier=None):
        with self.lock:
            if identifier:
                data = self.execution.docker_query('inventory', identifier=identifier)
            elif fresh or self.cached is None or time.monotonic() - self.cached_at > 3:
                self.cached = self.execution.docker_query('inventory')
                self.cached_at = time.monotonic()
                data = self.cached
            else:
                data = self.cached
            rows = []
            for original in data['containers']:
                row = original.copy()
                recipe = self.store.get('docker-recipes', row['group'])
                if recipe is None:
                    recipe = self.store.put('docker-recipes', dict(id=row['group'], **row['generated'], edited=False))
                elif not recipe.get('edited') and any(recipe.get(k) != v for k, v in row['generated'].items()):
                    recipe = self.store.put('docker-recipes', dict(id=row['group'], **row['generated'], edited=False))
                row['recipe'] = recipe
                cached = self.store.get('docker-images', row['cache_key']) if row['cache_key'] else None
                row['update_available'] = bool(cached and cached.get('target') and cached['target'] != row['image_id'])
                row['update_enabled'] = bool(recipe.get('command')) and not row['self_update']
                row.pop('generated', None)
                rows.append(row)
            return dict(containers=rows, warnings=data['warnings'], active_job=self.jobs.active)

    def container(self, identifier, fresh=False):
        row = next((row for row in self.listing(fresh, identifier=identifier if fresh else None)['containers'] if row['id'] == identifier), None)
        if not row:
            raise ValueError('Container no longer exists. Refresh the Docker table.')
        return row

    def edit(self, identifier, command, cwd, timeout):
        # Hold the same lock as job admission so an update cannot race with editing its recipe.
        with self.jobs.lock:
            if self.jobs.active:
                raise Busy('Wait for the current job to finish before editing an update command.')
            with self.lock:
                row = self.container(identifier)
                return self.store.put('docker-recipes', dict(id=row['group'], command=command, cwd=cwd,
                    timeout=timeout, edited=True, reason=''))

    def prepare(self, identifier):
        with self.lock:
            row = self.container(identifier, fresh=True)
            if not row['update_enabled']:
                raise ValueError('Update this Orca client using an external executor.' if row['self_update'] else
                                 'Edit and save an update command first.')
            recipe = row['recipe']
            snapshot = dict(container=identifier, group=row['group'], name=row['name'],
                            project=row['project'], command=recipe['command'], cwd=recipe['cwd'], timeout=recipe['timeout'])
            confirmation = dict(id=uuid.uuid4().hex, created=time.time(), **snapshot)
            # Short-lived confirmations bind execution to the exact command shown in the popup.
            for old in self.store.all('docker-confirmations'):
                if old['created'] < time.time() - 600:
                    self.store.delete('docker-confirmations', old['id'])
            return self.store.put('docker-confirmations', confirmation)

    def update(self, identifier, confirmation_id):
        with self.jobs.lock:
            with self.lock:
                confirmation = self.store.get('docker-confirmations', confirmation_id)
                if not confirmation or confirmation['container'] != identifier or confirmation['created'] < time.time() - 600:
                    raise ValueError('Update confirmation expired. Open the confirmation again.')
                row = self.container(identifier, fresh=True)
                recipe = row['recipe']
                if not row['update_enabled'] or row['group'] != confirmation['group'] or any(
                        recipe[k] != confirmation[k] for k in ('command', 'cwd', 'timeout')):
                    raise ValueError('Update command changed. Review and confirm it again.')
                action = dict(id='docker-update', name='Update ' + (row['project'] or row['name']),
                              command=confirmation['command'], cwd=confirmation['cwd'], timeout=confirmation['timeout'],
                              kind='docker-update', group=row['group'])
            # Lock order: job admission never happens while the discovery lock is held.
            job = self.jobs.start(action, after=self.invalidate)
            self.store.delete('docker-confirmations', confirmation_id)
            return job

    def lifecycle(self, identifier, operation):
        row = self.container(identifier, fresh=True)
        if operation not in LIFECYCLE.get(row['state'], set()) or row['managed']:
            raise ValueError('This operation is unavailable for the current container state or deployment manager.')
        if row['self_update'] and operation in ('stop', 'pause', 'restart'):
            raise ValueError('Control this Orca client using an external executor.')
        return self.jobs.start(dict(id='docker-' + operation, name=operation.title() + ' ' + row['name'],
            command=shlex.join(self.execution.docker_prefix + ['container', operation, identifier]),
            cwd='', timeout=120, kind='docker-control'), after=self.invalidate)

    def check(self):
        return self.jobs.start(dict(BUILTIN, kind='docker-check'), task=self._check, after=self.invalidate)

    def _check(self, append):
        append('Checking registry images for all existing containers on this client…\n')
        data = self.execution.docker_query('check', timeout=3600)
        by_key = {}
        for result in data['results']:
            previous = self.store.get('docker-images', result['id']) or {}
            # A failed refresh retains the last successful target; output records the failure.
            record = dict(previous, **result)
            if 'target' in result:
                record.pop('error', None)
                record['last_success'] = result['checked_at']
            self.store.put('docker-images', record)
            by_key[result['id']] = result
        failed = bool(data['warnings'])
        for row in data['containers']:
            result = by_key.get(row['cache_key'] or row['id'], {})
            if result.get('error'):
                detail = 'CHECK FAILED — ' + result['error']
                failed = True
            elif result.get('pinned'):
                detail = 'DIGEST PINNED — change the configured reference to select another image'
            else:
                detail = 'UPDATE AVAILABLE' if result.get('target') != row['image_id'] else 'UP TO DATE'
            append(f"{row['name']} ({row['image']}): {detail}\n")
        for warning in data['warnings']:
            append('Warning: ' + warning + '\n')
        append(f"Checked {len(data['containers'])} containers. Registry results saved on this client.\n")
        return 2 if failed else 0
