import os
from waitress import serve
from .app import create_app


def run(role=None):
    if role:
        os.environ['ORCA_ROLE'] = role
    app = create_app()
    serve(app, host=os.getenv('ORCA_HOST', '127.0.0.1'),
          port=int(os.getenv('ORCA_PORT', '8000' if app.config['ROLE'] == 'orchestrator' else '8001')), threads=8)


def orchestrator():
    run('orchestrator')


def client():
    run('client')
