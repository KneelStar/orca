import hmac
import os
import platform
import secrets
import subprocess
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, render_template, request, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from .execution import Execution
from .jobs import Busy, Jobs
from .store import Store


def create_app(overrides=None):
    load_dotenv(os.environ.get('ORCA_ENV_FILE', '.env'))
    app = Flask(__name__)
    role = os.getenv('ORCA_ROLE', 'orchestrator')
    app.config.update(
        ROLE=role, ADMIN_PASSWORD=os.getenv('ORCA_ADMIN_PASSWORD', ''),
        SECRET_KEY=os.getenv('ORCA_SESSION_SECRET', ''), CLIENT_TOKEN=os.getenv('ORCA_CLIENT_TOKEN', ''),
        ADMIN_TITLE=os.getenv('ORCA_ADMIN_TITLE', 'Your fleet'), PUBLIC_TITLE=os.getenv('ORCA_PUBLIC_TITLE', 'Your home base'),
        CLIENT_NAME=os.getenv('ORCA_CLIENT_NAME', platform.node()),
        DATABASE=os.getenv('ORCA_DATABASE', f'data/{role}.sqlite3'),
        SESSION_COOKIE_NAME='orca_' + role, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
        SESSION_COOKIE_SECURE=os.getenv('ORCA_COOKIE_SECURE', 'false').lower() == 'true',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8), MAX_CONTENT_LENGTH=65536,
    )
    if overrides:
        app.config.update(overrides)
    role = app.config['ROLE']
    if role not in ('client', 'orchestrator'):
        raise ValueError('ORCA_ROLE must be client or orchestrator.')
    if len(app.config['ADMIN_PASSWORD']) < 12 or len(app.config['SECRET_KEY']) < 32:
        raise ValueError('Set ORCA_ADMIN_PASSWORD (12+ characters) and ORCA_SESSION_SECRET (32+ characters).')
    if role == 'client' and len(app.config['CLIENT_TOKEN']) < 32:
        raise ValueError('Set ORCA_CLIENT_TOKEN to a random value of at least 32 characters.')
    password_hash = generate_password_hash(app.config['ADMIN_PASSWORD'])
    store = Store(app.config['DATABASE'])
    app.extensions['store'] = store
    execution = Execution() if role == 'client' else None
    jobs = Jobs(store, execution) if role == 'client' else None
    app.extensions['jobs'] = jobs
    failures = defaultdict(deque)
    auth_lock = threading.Lock()
    metrics_lock = threading.Lock()
    cached_metrics = {'at': 0, 'data': None}

    def authenticated():
        return session.get('admin') is True

    def token_authenticated():
        expected = app.config['CLIENT_TOKEN']
        provided = request.headers.get('Authorization', '')
        return role == 'client' and bool(expected) and hmac.compare_digest(provided.encode(), ('Bearer ' + expected).encode())

    def protect(machine=False):
        def decorator(fn):
            @wraps(fn)
            def wrapped(*args, **kwargs):
                if machine and token_authenticated():
                    jobs.prune_history()
                    return fn(*args, **kwargs)
                if not authenticated():
                    abort(401, 'Admin login required.')
                csrf()
                if jobs:
                    jobs.prune_history()
                return fn(*args, **kwargs)
            return wrapped
        return decorator

    def csrf():
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            expected = session.get('csrf', '')
            if not expected or not hmac.compare_digest(request.headers.get('X-CSRF-Token', '').encode(), expected.encode()):
                abort(403, 'Reload the page and try again.')

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            abort(400, 'Expected a JSON object.')
        return data

    def text(data, field, limit=200, optional=False):
        value = data.get(field, '')
        if not isinstance(value, str) or len(value) > limit or (not optional and not value.strip()):
            abort(400, f'Invalid {field}.')
        return value.strip()

    def url(value, base=False):
        try:
            parsed = urlsplit(value)
            valid = parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password
            _ = parsed.port
            if base:
                valid = valid and parsed.path in ('', '/') and not parsed.query and not parsed.fragment
            if not valid:
                raise ValueError()
        except ValueError:
            abort(400, 'Use an HTTP or HTTPS address, without credentials' + (' or a path.' if base else '.'))
        return value.rstrip('/') if base else value

    def require_role(expected):
        if role != expected:
            abort(404)

    def record(kind, key):
        result = store.get(kind, key)
        if not result:
            abort(404, 'Item no longer exists.')
        return result

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https: http:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get('/')
    def home():
        return render_template('index.html')

    @app.get('/api/session')
    def session_info():
        session.setdefault('csrf', secrets.token_urlsafe(32))
        return jsonify(admin=authenticated(), csrf=session['csrf'], role=role,
                       admin_title=app.config['ADMIN_TITLE'], public_title=app.config['PUBLIC_TITLE'],
                       client_name=app.config['CLIENT_NAME'] if authenticated() else None)

    @app.post('/api/login')
    def login():
        csrf()
        key = request.remote_addr
        now = time.monotonic()
        with auth_lock:
            for address in list(failures):
                while failures[address] and now - failures[address][0] > 300:
                    failures[address].popleft()
                if not failures[address]:
                    del failures[address]
            attempts = failures[key]
            if len(attempts) >= 10:
                abort(429, 'Too many attempts. Try again in five minutes.')
            attempts.append(now)
        supplied = body().get('password', '')
        if not isinstance(supplied, str) or len(supplied) > 512:
            abort(400, 'Invalid password.')
        if not check_password_hash(password_hash, supplied):
            abort(401, 'Incorrect password.')
        with auth_lock:
            failures.pop(key, None)
        session.clear()
        session.update(admin=True, csrf=secrets.token_urlsafe(32))
        session.permanent = True
        return session_info()

    @app.post('/api/logout')
    @protect()
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get('/api/shortcuts')
    def shortcuts():
        require_role('orchestrator')
        return jsonify([s for s in store.all('shortcuts') if s['visibility'] == 'shared' or authenticated()])

    def save_order(kind):
        try:
            store.reorder(kind, body().get('ids'))
        except ValueError as error:
            abort(400, str(error))
        return jsonify(ok=True)

    @app.put('/api/shortcuts/order')
    @protect()
    def order_shortcuts():
        require_role('orchestrator')
        return save_order('shortcuts')

    @app.put('/api/clients/order')
    @protect()
    def order_clients():
        require_role('orchestrator')
        return save_order('clients')

    @app.post('/api/shortcuts')
    @app.put('/api/shortcuts/<key>')
    @protect()
    def save_shortcut(key=None):
        require_role('orchestrator')
        if key:
            record('shortcuts', key)
        data = body()
        visibility = data.get('visibility', 'admin')
        if visibility not in ('shared', 'admin'):
            abort(400, 'Invalid visibility.')
        open_in = data.get('open_in', 'new_tab')
        if open_in not in ('new_tab', 'same_tab'):
            abort(400, 'Invalid tab preference.')
        return jsonify(store.put('shortcuts', dict(id=key or uuid.uuid4().hex, name=text(data, 'name'),
                    url=url(text(data, 'url', 2048)), visibility=visibility, open_in=open_in)))

    @app.delete('/api/shortcuts/<key>')
    @protect()
    def delete_shortcut(key):
        require_role('orchestrator')
        record('shortcuts', key)
        store.delete('shortcuts', key)
        return jsonify(ok=True)

    @app.get('/api/clients')
    @protect()
    def clients():
        require_role('orchestrator')
        return jsonify([{k: v for k, v in c.items() if k != 'token'} for c in store.all('clients')])

    @app.post('/api/clients')
    @app.put('/api/clients/<key>')
    @protect()
    def save_client(key=None):
        require_role('orchestrator')
        previous = record('clients', key) if key else {}
        data = body()
        token = text(data, 'token', 512, optional=bool(key)) or previous.get('token', '')
        if len(token) < 32:
            abort(400, 'Client token must contain at least 32 characters.')
        item = dict(id=key or uuid.uuid4().hex, name=text(data, 'name'), url=url(text(data, 'url', 2048), base=True), token=token)
        store.put('clients', item)
        return jsonify({k: v for k, v in item.items() if k != 'token'})

    @app.delete('/api/clients/<key>')
    @protect()
    def delete_client(key):
        require_role('orchestrator')
        record('clients', key)
        store.delete('clients', key)
        return jsonify(ok=True)

    # Only these client endpoints can be reached through the orchestrator.
    @app.route('/api/clients/<key>/remote/<path:endpoint>', methods=['GET', 'POST', 'PUT', 'DELETE'])
    @protect()
    def remote(key, endpoint):
        require_role('orchestrator')
        parts = endpoint.split('/')
        allowed = ((endpoint == 'metrics' and request.method == 'GET') or
                   (endpoint == 'actions' and request.method in ('GET', 'POST')) or
                   (len(parts) == 2 and parts[0] == 'actions' and request.method in ('PUT', 'DELETE')) or
                   (len(parts) == 3 and parts[0] == 'actions' and parts[2] == 'run' and request.method == 'POST') or
                   (endpoint == 'jobs' and request.method == 'GET') or
                   (len(parts) == 2 and parts[0] == 'jobs' and request.method == 'GET'))
        if not allowed or any(not p.replace('-', '').isalnum() for p in parts):
            abort(404)
        client = record('clients', key)
        payload = body() if request.method in ('POST', 'PUT') else None
        try:
            with requests.Session() as transport:
                transport.trust_env = False
                with transport.request(request.method, client['url'] + '/api/host/' + endpoint,
                                       headers={'Authorization': 'Bearer ' + client['token']}, json=payload,
                                       timeout=(3, 10), allow_redirects=False, stream=True) as response:
                    if 300 <= response.status_code < 400:
                        abort(502, 'Client redirected the request. Check its configured address.')
                    content = bytearray()
                    for chunk in response.iter_content(16384):
                        content.extend(chunk)
                        if len(content) > 1048576:
                            abort(502, 'Client response exceeded the size limit.')
                    import json
                    try:
                        value = json.loads(content)
                    except (ValueError, UnicodeDecodeError):
                        abort(502, 'Client returned an invalid response.')
                    # A remote auth failure must not log out the orchestrator session.
                    if response.status_code in (401, 403):
                        abort(502, 'Client authentication failed. Check the saved client token.')
                    return jsonify(value), response.status_code
        except requests.RequestException:
            abort(502, 'Client is unreachable. Check its address, service, and network access.')

    @app.get('/api/host/metrics')
    @protect(machine=True)
    def metrics():
        require_role('client')
        with metrics_lock:
            if time.monotonic() - cached_metrics['at'] > 2:
                try:
                    data = execution.metrics()
                except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
                    abort(502, str(error) if isinstance(error, RuntimeError) else 'Unable to collect host metrics.')
                cached_metrics['data'] = dict(data, name=app.config['CLIENT_NAME'], timestamp=time.time())
                cached_metrics['at'] = time.monotonic()
            return jsonify(cached_metrics['data'])

    @app.get('/api/host/actions')
    @protect(machine=True)
    def actions():
        require_role('client')
        return jsonify(store.all('actions'))

    @app.put('/api/host/actions/order')
    @protect(machine=True)
    def order_actions():
        require_role('client')
        return save_order('actions')

    @app.post('/api/host/actions')
    @app.put('/api/host/actions/<key>')
    @protect(machine=True)
    def save_action(key=None):
        require_role('client')
        if key:
            record('actions', key)
        data = body()
        timeout = data.get('timeout', 3600)
        if type(timeout) is not int or not 1 <= timeout <= 86400:
            abort(400, 'Timeout must be between 1 and 86400 seconds.')
        cwd = text(data, 'cwd', 4096, optional=True)
        if cwd and execution.mode == 'local' and not Path(cwd).is_dir():
            abort(400, 'Working directory does not exist on this client.')
        return jsonify(store.put('actions', dict(id=key or uuid.uuid4().hex, name=text(data, 'name'),
            command=text(data, 'command', 16384), cwd=cwd, timeout=timeout)))

    @app.delete('/api/host/actions/<key>')
    @protect(machine=True)
    def delete_action(key):
        require_role('client')
        record('actions', key)
        store.delete('actions', key)
        return jsonify(ok=True)

    @app.post('/api/host/actions/<key>/run')
    @protect(machine=True)
    def run_action(key):
        require_role('client')
        try:
            return jsonify(jobs.start(record('actions', key))), 202
        except Busy as error:
            abort(409, str(error))

    @app.get('/api/host/jobs')
    @protect(machine=True)
    def history():
        require_role('client')
        return jsonify([{k: v for k, v in j.items() if k != 'output'} for j in store.all('jobs')][-30:][::-1])

    @app.get('/api/host/jobs/<key>')
    @protect(machine=True)
    def job(key):
        require_role('client')
        return jsonify(record('jobs', key))

    return app
