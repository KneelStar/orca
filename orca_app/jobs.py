"""One host action at a time; output and terminal status survive page reloads."""
import codecs
import calendar
from datetime import datetime, timezone
import os
import signal
import subprocess
import threading
import time
import uuid


class Busy(Exception):
    pass


class Jobs:
    def __init__(self, store, execution):
        self.store = store
        self.execution = execution
        self.lock = threading.RLock()
        self.active = None
        self.cleanup_lock = threading.Lock()
        self.last_cleanup = None
        for job in store.all('jobs'):
            if job['status'] == 'running':
                job.update(status='interrupted', finished=time.time())
                job['output'] += '\nOrca restarted before this action finished. Check host state before rerunning.\n'
                store.put('jobs', job)
        self.prune_history()

    def prune_history(self):
        """Expire finished runs after six calendar months, at most hourly."""
        with self.cleanup_lock:
            now = time.monotonic()
            if self.last_cleanup is not None and now - self.last_cleanup < 3600:
                return
            today = datetime.now(timezone.utc)
            month_index = today.year * 12 + today.month - 1 - 6
            year, month = divmod(month_index, 12)
            month += 1
            cutoff = today.replace(year=year, month=month,
                                   day=min(today.day, calendar.monthrange(year, month)[1]))
            self.store.prune_jobs(cutoff.timestamp())
            self.last_cleanup = now

    def start(self, action, task=None, after=None):
        with self.lock:
            if self.active:
                raise Busy('Another action is already running on this client.')
            job = dict(id=uuid.uuid4().hex, name=action['name'], action_id=action['id'],
                       status='running', started=time.time(), finished=None, exit_code=None, output='')
            job.update({key: action[key] for key in ('kind', 'group', 'command', 'cwd') if key in action})
            self.store.put('jobs', job)
            self.active = job['id']
            threading.Thread(target=self._run, args=(job, action.copy(), task, after), daemon=True).start()
            return job.copy()

    def _run(self, job, action, task=None, after=None):
        process = None
        timer = None
        timed_out = threading.Event()
        def append(text):
            job['output'] = (job['output'] + text)[-262144:]
            self.store.put('jobs', job)
        def terminate():
            timed_out.set()
            try:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=10)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError, subprocess.SubprocessError):
                pass
        try:
            if task:
                code = task(append)
                job.update(exit_code=code, status='succeeded' if code == 0 else 'failed')
            else:
                # Do not expose Orca credentials to configured commands through their environment.
                env = {k: v for k, v in os.environ.items() if not k.upper().startswith('ORCA_')}
                process = subprocess.Popen(**self.execution.command(action),
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                           env=env, start_new_session=os.name != 'nt')
                timer = threading.Timer(action['timeout'] + (15 if self.execution.mode == 'ssh' else 0), terminate)
                timer.daemon = True
                timer.start()
                decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
                while chunk := process.stdout.read1(4096):
                    append(decoder.decode(chunk))
                append(decoder.decode(b'', final=True))
                code = process.wait()
                job.update(exit_code=code, status='timed_out' if timed_out.is_set() or (self.execution.mode == 'ssh' and code == 124) else ('succeeded' if code == 0 else 'failed'))
                if timed_out.is_set():
                    append('\nAction exceeded its time limit.\n')
        except Exception as error:
            job['status'] = 'failed'
            append(f'Unable to run action: {error}\n')
        finally:
            if timer:
                timer.cancel()
            if process and process.stdout:
                process.stdout.close()
            if after:
                try:
                    after()
                except Exception as error:
                    append(f'Unable to refresh Docker data: {error}\n')
            job['finished'] = time.time()
            self.store.put('jobs', job)
            with self.lock:
                self.active = None
