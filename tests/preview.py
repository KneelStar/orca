"""Disposable localhost preview with an independent client. Never binds publicly."""
import json
import secrets
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from waitress import serve
from orca_app.app import create_app

folder = tempfile.TemporaryDirectory(prefix='orca-preview-')
base = dict(ADMIN_PASSWORD='orca-preview-local', SECRET_KEY=secrets.token_hex(32), CLIENT_TOKEN=secrets.token_hex(32))
client = create_app(dict(base, ROLE='client', DATABASE=str(Path(folder.name)/'client.sqlite3'),
                         CLIENT_NAME='Local test client', SESSION_COOKIE_NAME='orca_preview_client'))
main = create_app(dict(base, ROLE='orchestrator', DATABASE=str(Path(folder.name)/'main.sqlite3'),
                       SESSION_COOKIE_NAME='orca_preview_main'))
main.extensions['store'].put('clients',dict(id='local-client',name='Local test client',url='http://127.0.0.1:8766',token=base['CLIENT_TOKEN']))
threading.Thread(target=lambda:serve(client,host='127.0.0.1',port=8766),daemon=True).start()
print('Disposable preview ready on localhost ports 8765 and 8766.',flush=True)
serve(main,host='127.0.0.1',port=8765)
