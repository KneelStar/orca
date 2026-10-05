// Standalone browser regressions with hostile client responses and no real host actions.
const {chromium}=require(process.env.ORCA_PLAYWRIGHT || 'playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const http=require('node:http');
const path=require('node:path');

(async()=>{
  const root=path.resolve(__dirname,'..'),requests=[],errors=[];
  const hostile='record"><img data-injected src="x" onerror="window.injected=true">';
  const timeout='<img data-injected src="x" onerror="window.injected=true">';
  const action={id:hostile,name:'Untrusted action',command:'printf private-command',cwd:'/private-directory',timeout};
  const job={id:'job'+hostile,name:'Private job',status:'succeeded',started:1,output:'private-job-output',exit_code:0};
  const container={id:'docker'+hostile,name:'Untrusted container',state:'running',health:'healthy',image:'example:test',tag:'test',
    update_enabled:true,recipe:{command:'printf private-docker-command',cwd:'/private-docker-directory',timeout:5}};
  let admin=true,holdClients=false,remoteAuthFailure=false,pendingClients,releaseClients;
  const server=http.createServer(async(req,res)=>{
    requests.push({url:req.url,method:req.method});
    if(req.url.startsWith('/api/')){
      if(req.url==='/api/login'){
        res.writeHead(401,{'Content-Type':'application/json'});res.end(JSON.stringify({error:'Incorrect password.'}));return;
      }
      if(!admin && !['/api/session','/api/shortcuts'].includes(req.url)){
        res.writeHead(401,{'Content-Type':'application/json'});res.end(JSON.stringify({error:'Admin login required.'}));return;
      }
      if(remoteAuthFailure && req.url.includes('/remote/')){
        res.writeHead(502,{'Content-Type':'application/json'});res.end(JSON.stringify({error:'Client authentication failed.'}));return;
      }
      let value;
      if(req.url==='/api/session')value={admin,csrf:'test-csrf',role:'orchestrator',admin_title:'Private fleet',public_title:'Public links'};
      else if(req.url==='/api/logout'){admin=false;value={ok:true};}
      else if(req.url==='/api/shortcuts')value=admin?[{id:'private',name:'Private shortcut',url:'https://example.com/private',visibility:'admin'}]:[];
      else if(req.url==='/api/clients'){
        if(holdClients){pendingClients?.();await new Promise(resolve=>releaseClients=resolve);}
        value=[{id:'test-client',name:'Private client',url:'http://private-client:8001'}];
      }
      else if(req.url.endsWith('/metrics'))value={cpu:1,ram:2,disks:[],os:'Private operating system'};
      else if(req.url.endsWith('/orca-actions'))value=[{id:'builtin'+hostile,name:'Untrusted builtin',builtin:true}];
      else if(req.url.endsWith('/actions') && req.method==='GET')value=[action,{id:'normal',name:'Normal action',command:'printf harmless',timeout:5}];
      else if(req.url.endsWith('/jobs'))value=[job];
      else if(req.url.includes('/jobs/'))value=job;
      else if(req.url.endsWith('/docker'))value={containers:[container],warnings:[],active_job:null};
      else if(req.url.endsWith('/prepare'))value={id:'confirmation',name:container.name,command:container.recipe.command,cwd:container.recipe.cwd};
      else if(req.url.endsWith('/run') || req.url.endsWith('/control'))value={...job,id:'new-job'};
      else value={ok:true};
      res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(value));return;
    }
    const files={'/':'orca_app/templates/index.html','/static/app.js':'orca_app/static/app.js',
      '/static/docker.js':'orca_app/static/docker.js','/static/app.css':'orca_app/static/app.css','/static/favicon.svg':'orca_app/static/favicon.svg'};
    const file=files[req.url];
    if(!file){res.writeHead(404);res.end();return;}
    const type=file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.svg')?'image/svg+xml':'text/html';
    res.writeHead(200,{'Content-Type':type});res.end(fs.readFileSync(path.join(root,file)));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  try{
    browser=await chromium.launch({headless:true,executablePath:process.env.ORCA_BROWSER||undefined});
    const page=await browser.newPage();page.on('pageerror',e=>errors.push(e.message));
    await page.route('https://example.com/**',route=>route.abort());
    async function assertPrivateCleared(){
      const remaining=await page.evaluate(()=>({text:document.body.textContent,inputs:[...document.querySelectorAll('input,textarea')].map(el=>el.value),
        clients:state.clients,shortcuts:state.shortcuts,actions:state.actions,orcaActions:state.orcaActions,job:state.job,historyJob:state.historyJob,docker:dockerState.rows}));
      assert.doesNotMatch(remaining.text,/private-client|private-command|private-job-output|private-docker|Private shortcut|Private client/);
      assert.ok(!remaining.inputs.includes('private-token-draft'));
      for(const key of ['clients','shortcuts','actions','orcaActions','docker'])assert.deepEqual(remaining[key],[]);
      assert.equal(remaining.job,null);assert.equal(remaining.historyJob,null);
      assert.equal(await page.evaluate(()=>state.session.admin),false);
      assert.equal(await page.locator('#error').isVisible(),false);
    }
    async function openPrivateView(){
      admin=true;await page.reload();await page.locator('.server').click();
      await page.getByRole('button',{name:action.name,exact:true}).click();
      await page.waitForFunction(()=>document.querySelector('#output').textContent==='private-job-output');
    }
    await page.goto('http://127.0.0.1:'+server.address().port);
    await page.locator('.server').click();
    await page.locator('#actions .action-shell').first().waitFor();
    assert.equal(await page.locator('[data-injected]').count(),0);
    assert.equal(await page.locator('#actions .action-run').first().getAttribute('data-run'),action.id);
    assert.equal(await page.locator('#orca-actions .action-run').first().getAttribute('data-run'),'builtin'+hostile);
    assert.equal(await page.locator('#job-history option').nth(1).getAttribute('value'),job.id);
    await page.getByRole('button',{name:'Information about '+action.name,exact:true}).click();
    assert.equal(await page.locator('[data-injected]').count(),0);
    assert.match(await page.locator('#action-confirm').textContent(),/Timeout: <img data-injected/);
    // Canceling a reorder restores focus without using a hostile ID as a CSS selector.
    await page.locator('#actions .drag-handle').first().press('ArrowLeft');
    assert.equal(await page.locator('#actions .drag-handle').first().evaluate(el=>document.activeElement===el),true);
    await page.getByRole('button',{name:action.name,exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#output').textContent==='private-job-output');
    assert.ok(requests.some(r=>r.url==='/api/clients/test-client/remote/actions/'+encodeURIComponent(action.id)+'/run'));
    await page.getByRole('tab',{name:'Recent actions',exact:true}).click();
    await page.locator('#job-history').selectOption(job.id);
    await page.waitForFunction(()=>document.querySelector('#history-output').textContent==='private-job-output');
    assert.ok(requests.some(r=>r.url==='/api/clients/test-client/remote/jobs/'+encodeURIComponent(job.id)));
    await page.getByRole('tab',{name:'Docker',exact:true}).click();
    await page.locator('#docker-rows tr').waitFor();
    assert.equal(await page.locator('[data-injected]').count(),0);
    assert.equal(await page.getByRole('button',{name:'Stop '+container.name,exact:true}).getAttribute('data-container'),container.id);
    await page.getByRole('button',{name:'Edit update command for '+container.name,exact:true}).click();
    assert.equal(await page.locator('#docker-update-command').inputValue(),container.recipe.command);
    await page.getByRole('button',{name:'Close Docker dialog',exact:true}).click();
    // Preserve sensitive drafts/output to prove sign-out removes hidden DOM too.
    await page.getByRole('button',{name:'+ Add client',exact:true}).click();
    await page.locator('#client-editor input[name="token"]').fill('private-token-draft');
    await page.getByRole('button',{name:'Sign out',exact:true}).click();
    await page.getByRole('heading',{name:'Public links',exact:true}).waitFor();
    await assertPrivateCleared();
    // An in-flight admin load must not repopulate private state after logout.
    admin=true;await page.reload();await page.locator('.server').waitFor();
    holdClients=true;
    const clientRequest=new Promise(resolve=>pendingClients=resolve);
    await page.evaluate(()=>{window.pendingLoad=load();});await clientRequest;
    await page.getByRole('button',{name:'Sign out',exact:true}).click();
    await page.getByRole('heading',{name:'Public links',exact:true}).waitFor();
    releaseClients();await page.evaluate(()=>window.pendingLoad);
    assert.equal(await page.locator('#servers').textContent(),'');
    assert.deepEqual(await page.evaluate(()=>state.clients),[]);
    holdClients=false;
    // A remote client auth failure and a bad login password are not local session expiry.
    await openPrivateView();remoteAuthFailure=true;
    await page.getByRole('button',{name:action.name,exact:true}).click();
    await page.getByRole('alert').filter({hasText:'Client authentication failed.'}).waitFor();
    assert.equal(await page.evaluate(()=>state.session.admin),true);
    assert.equal(await page.locator('#output').textContent(),'private-job-output');
    remoteAuthFailure=false;
    await page.evaluate(()=>api('/api/login','POST',{password:'wrong'}).catch(fail));
    assert.equal(await page.evaluate(()=>state.session.admin),true);
    assert.equal(await page.locator('#error').textContent(),'Incorrect password.');
    // Protected 401 responses erase private data and obtain a public session without reload.
    await page.getByRole('button',{name:'+ Add client',exact:true}).click();
    await page.locator('#client-editor input[name="token"]').fill('private-token-draft');
    admin=false;await page.evaluate(()=>refreshMetrics());
    await page.getByRole('heading',{name:'Public links',exact:true}).waitFor();
    await assertPrivateCleared();
    // Even an expired-session Sign out returning 401 must clean up the existing page.
    await openPrivateView();admin=false;
    await page.getByRole('button',{name:'Sign out',exact:true}).click();
    await page.getByRole('heading',{name:'Public links',exact:true}).waitFor();
    await assertPrivateCleared();
    assert.deepEqual(errors,[]);
    console.log('Frontend security passed: hostile markup, encoded IDs, safe reorder focus, logout/expiry cleanup, stale responses, and remote auth isolation.');
  }finally{
    releaseClients?.();await browser?.close();await new Promise(resolve=>server.close(resolve));
  }
})().catch(error=>{console.error(error);process.exit(1)});
