"""Disposable localhost preview with an independent client. Never binds publicly."""
import json
import os
import secrets
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from waitress import serve
from orca_app.app import create_app

folder = tempfile.TemporaryDirectory(prefix='orca-preview-')
port = int(os.getenv('ORCA_PREVIEW_PORT', '8765'))
client_port = int(os.getenv('ORCA_PREVIEW_CLIENT_PORT', '8766'))
base = dict(ADMIN_PASSWORD='orca-preview-local', SECRET_KEY=secrets.token_hex(32), CLIENT_TOKEN=secrets.token_hex(32))
client = create_app(dict(base, ROLE='client', DATABASE=str(Path(folder.name)/'client.sqlite3'),
                         CLIENT_NAME='Local test client', SESSION_COOKIE_NAME='orca_preview_client'))
if os.getenv('ORCA_PREVIEW_DOCKER_FIXTURE') == 'true':
    from docker_fixture import install_docker_fixture
    install_docker_fixture(client)
main = create_app(dict(base, ROLE='orchestrator', DATABASE=str(Path(folder.name)/'main.sqlite3'),
                       SESSION_COOKIE_NAME='orca_preview_main'))
main.extensions['store'].put('clients',dict(id='local-client',name='Local test client',url=f'http://127.0.0.1:{client_port}',token=base['CLIENT_TOKEN']))
threading.Thread(target=lambda:serve(client,host='127.0.0.1',port=client_port),daemon=True).start()
print(f'Starting disposable preview on localhost ports {port} and {client_port}.',flush=True)
serve(main,host='127.0.0.1',port=port)
