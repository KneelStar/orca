"""Opt-in disposable Docker simulation for browser tests; no Docker daemon access."""
import copy
import hashlib
import time


def install_docker_fixture(client):
    service=client.extensions['docker']
    rows=[]
    states=[('immich-server','running','healthy'),('immich-redis','running',None),('backup','exited',None),
            ('worker','paused',None),('importer','created',None),('gateway','restarting',None),('old-task','dead',None)]
    for index,(name,state,health) in enumerate(states):
        group='immich' if index<2 else name
        key=hashlib.sha256(name.encode()).hexdigest()
        rows.append(dict(id=str(index+1)*64,name=name,tag='latest' if index==0 else '7' if index==1 else 'stable',
            image=name+':latest',image_id='sha256:old' if index==0 else 'sha256:current',group=group,
            project='immich' if index<2 else '',state=state,health=health,exit_code=0,oom=False,
            cpu='2.50%' if state=='running' else None,memory='300MiB' if state=='running' else None,
            platform=['linux','amd64','',''],platform_error='',cache_key=key,managed=False,self_update=False,
            generated=dict(command='docker compose -p immich -f /srv/immich/custom.yaml pull && docker compose -p immich -f /srv/immich/custom.yaml up -d' if index<2 else '',
                           cwd='/srv/immich' if index<2 else '',timeout=3600,reason='' if index<2 else 'Provide an update command.')))
    def query(operation,**kwargs):
        result=dict(containers=copy.deepcopy(rows),warnings=[],endpoint='fixture')
        if operation=='check':
            time.sleep(.4)
            result['results']=[dict(id=row['cache_key'],image=row['image'],platform=row['platform'],
                checked_at=time.time(),target='sha256:new' if row['name']=='immich-server' else 'sha256:current') for row in rows]
        return result
    original=service.execution.command
    def command(action):
        if action.get('kind')=='docker-control':
            operation=action['id'].removeprefix('docker-')
            identifier=action['command'].split()[-1]
            row=next(row for row in rows if row['id']==identifier)
            row['state']={'start':'running','stop':'exited','pause':'paused','unpause':'running','restart':'running'}[operation]
            return dict(args='printf "Simulated container control"',shell=True,cwd=None)
        if action.get('kind')=='docker-update':
            for row in rows:
                if row['group']==action['group'] and row['name']=='immich-server':
                    row['id']='f'*64;row['image_id']='sha256:new'
        return original(action)
    service.execution.docker_query=query
    service.execution.command=command
