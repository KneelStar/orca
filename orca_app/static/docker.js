'use strict';
const dockerState={tab:'actions',rows:[],loaded:false,loading:false,activeJob:null,dialogJob:null,dialogGeneration:0,request:0};
const dockerIcons={
  start:'<path d="m8 5 11 7-11 7Z"/>', stop:'<path d="M6 6h12v12H6z"/>',
  pause:'<path d="M6 5h4v14H6zm8 0h4v14h-4z"/>', unpause:'<path d="M5 5h3v14H5zm6 0 10 7-10 7Z"/>',
  restart:'<path d="M18.8 6.2A9 9 0 1 0 21 12h-2a7 7 0 1 1-1.6-4.5L14 11h8V3Z"/>',
  update:'<path d="m12 3-7 7 1.4 1.4L11 6.8V17h2V6.8l4.6 4.6L19 10ZM4 19v2h16v-2Z"/>'
};
function resetDocker(){
  dockerState.rows=[];dockerState.loaded=false;dockerState.loading=false;dockerState.activeJob=null;dockerState.dialogJob=null;dockerState.request++;
  $('#docker-dialog').close();$('#docker-rows').innerHTML='';$('#docker-count').textContent='';
  $('#docker-message').textContent='Open this tab to load containers.';
  setWorkspaceTab('actions');
}
function setWorkspaceTab(name){
  dockerState.tab=name;
  for(const tab of ['actions','docker']){
    const selected=tab===name,button=$('#'+tab+'-tab');
    button.classList.toggle('active',selected);button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;
  }
  show('#work-grid',name==='actions');show('#docker-panel',name==='docker');
  if(name==='docker')refreshDocker();
}
$('#actions-tab').onclick=()=>setWorkspaceTab('actions');
$('#docker-tab').onclick=()=>setWorkspaceTab('docker');
$('.workspace-tabs').onkeydown=event=>{
  if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
  event.preventDefault();const next=event.key==='Home'?'actions':event.key==='End'?'docker':dockerState.tab==='actions'?'docker':'actions';
  setWorkspaceTab(next);$('#'+next+'-tab').focus();
};
async function refreshDocker(force=false){
  if(!isAdmin()||!state.selected||(!force && dockerState.tab!=='docker')||document.hidden||dockerState.loading)return;
  const generation=state.generation,request=++dockerState.request,path=remotePath('docker');dockerState.loading=true;
  if(!dockerState.loaded)$('#docker-message').textContent='Loading containers…';
  try{
    const data=await api(path);
    if(generation!==state.generation||request!==dockerState.request)return;
    dockerState.rows=data.containers;dockerState.loaded=true;dockerState.activeJob=data.active_job;
    $('#docker-message').textContent=data.warnings.join(' · ') || (data.containers.length?'':'No containers on this Docker endpoint.');
    $('#docker-count').textContent=`${data.containers.length} total`;
    if(data.active_job && state.job?.id!==data.active_job){
      const job=await api(remotePath('jobs/'+data.active_job));
      if(generation!==state.generation||request!==dockerState.request)return;
      state.job=job;state.quickCleared=false;renderJob();renderActions();
    }
    renderDocker();
  }catch(error){
    if(generation!==state.generation||request!==dockerState.request)return;
    // Do not leave stale healthy dots or actionable rows visible after discovery fails.
    dockerState.rows=[];dockerState.loaded=false;$('#docker-count').textContent='';
    $('#docker-message').textContent=error.message;renderDocker();
  }finally{if(generation===state.generation&&request===dockerState.request)dockerState.loading=false;}
}
function dockerBusy(){return state.starting||state.job?.status==='running'||!!dockerState.activeJob;}
function containerState(row){
  if(row.state==='running')return row.health==='healthy'?['green','Running · Healthy']:row.health==='unhealthy'?['red','Running · Unhealthy']:row.health==='starting'?['amber','Running · Health check starting']:['blue','Running · No health check'];
  if(row.state==='paused')return ['purple','Paused'+(row.health?' · '+row.health:'')];
  if(row.state==='restarting')return ['amber','Restarting'];
  if(row.state==='exited')return row.oom?['red','Stopped · Out of memory']:row.exit_code?['red',`Stopped · Exit ${row.exit_code}`]:['gray','Stopped · Exit 0'];
  return ({created:['gray','Created'],removing:['gray','Removing'],dead:['red','Dead']})[row.state]||['gray','Unknown'];
}
function renderDocker(){
  const focused=document.activeElement?.closest('#docker-rows button');
  const focusKey=focused?.dataset.focusKey;
  const busy=dockerBusy();
  $('#docker-rows').innerHTML=dockerState.rows.map(row=>{
    const [color,label]=containerState(row);
    const active=row.state==='running'||row.state==='restarting'||row.state==='paused';
    const startStop=active?'stop':'start',pause=row.state==='paused'?'unpause':'pause';
    const operations=({running:['stop','pause','restart'],paused:['unpause'],created:['start'],exited:['start'],restarting:['stop']})[row.state]||[];
    const button=(operation,name,enabled,extra='')=>`<button class="icon-button ${extra}" data-container="${row.id}" data-docker-operation="${operation}" data-focus-key="${row.id}-${operation}" aria-label="${name} ${escapeHTML(row.name)}" title="${name}" ${enabled?'':'disabled'}>${operation==='edit'?pencilIcon:`<svg viewBox="0 0 24 24" aria-hidden="true">${dockerIcons[operation]}</svg>`}</button>`;
    const lifecycle=(operation,name)=>button(operation,name,!busy&&!row.managed&&!(row.self_update&&['stop','pause','restart'].includes(operation))&&operations.includes(operation));
    return `<tr><td class="docker-name"><span title="${escapeHTML(row.image)}">${escapeHTML(row.name)}<span class="muted">@${escapeHTML(row.tag)}</span></span>${row.project?`<small class="muted">${escapeHTML(row.project)}</small>`:''}</td><td><span class="state-indicator"><button class="state-dot ${color}" aria-label="${escapeHTML(label)}" aria-describedby="state-${row.id}" data-focus-key="${row.id}-state"><span aria-hidden="true"></span></button><span role="tooltip" id="state-${row.id}" class="state-tooltip">${escapeHTML(label)}</span></span></td><td class="docker-stat">${escapeHTML(row.cpu||'—')}</td><td class="docker-stat">${escapeHTML(row.memory||'—')}</td><td><div class="docker-controls">${lifecycle(startStop,active?'Stop':'Start')}${lifecycle(pause,pause==='unpause'?'Resume':'Pause')}${lifecycle('restart','Restart')}<span class="update-group">${button('update','Update',!busy&&row.update_enabled,row.update_available?'update-available':'')}${button('edit','Edit update command for',!busy)}</span></div></td></tr>`;
  }).join('');
  if(focusKey){const target=[...$('#docker-rows').querySelectorAll('button')].find(button=>button.dataset.focusKey===focusKey);target?.focus({preventScroll:true});}
}
function openDockerDialog(title,body){
  $('#docker-dialog-title').textContent=title;$('#docker-dialog-body').innerHTML=body;
  show('#docker-dialog-error',false);dockerState.dialogGeneration=state.generation;dockerState.dialogJob=null;
  if(!$('#docker-dialog').open)$('#docker-dialog').showModal();
}
function dialogError(error){$('#docker-dialog-error').textContent=error.message;show('#docker-dialog-error',true);}
$('#docker-dialog-close').onclick=()=>$('#docker-dialog').close();
function editDocker(row){
  const recipe=row.recipe;
  openDockerDialog('Edit update command',`<p class="muted">${row.project?`Shared by all containers in project ${escapeHTML(row.project)}.`:`Update command for ${escapeHTML(row.name)}.`}</p>${row.self_update?'<p class="muted">Update this Orca client using an external executor.</p>':''}${recipe.reason?`<p class="muted">${escapeHTML(recipe.reason)}</p>`:''}<form id="docker-recipe-form"><div class="fields">${input('Working directory','cwd',recipe.cwd||'','text',false)}${input('Timeout (seconds)','timeout',recipe.timeout||3600,'number')}<div class="full"><label for="docker-update-command">Update command</label><textarea id="docker-update-command" name="command" rows="6" required>${escapeHTML(recipe.command||'')}</textarea></div></div><button class="primary">Save update command</button></form>`);
  const generation=state.generation,path=remotePath('docker/'+row.id+'/recipe');
  $('#docker-recipe-form').onsubmit=async event=>{
    event.preventDefault();const button=event.submitter;button.disabled=true;
    try{
      const data=Object.fromEntries(new FormData(event.target));data.timeout=Number(data.timeout);
      await api(path,'PUT',data);
      if(generation!==state.generation)return;
      $('#docker-dialog').close();await refreshDocker(true);
    }catch(error){if(generation===state.generation)dialogError(error);}finally{button.disabled=false;}
  };
}
async function prepareDockerUpdate(row){
  if(dockerBusy())return;
  const generation=state.generation,path=remotePath('docker/'+row.id);
  openDockerDialog('Update '+(row.project||row.name),'<p class="muted">Loading the current update command…</p>');
  try{
    const prepared=await api(path+'/prepare','POST',{});
    if(generation!==state.generation||!$('#docker-dialog').open)return;
    openDockerDialog('Update '+(prepared.project||prepared.name),`<p>${prepared.project?`This updates project <strong>${escapeHTML(prepared.project)}</strong>. Multiple containers may be recreated and stopped services may start.`:'This runs the saved update command. It may affect other containers or services.'}</p><p class="muted docker-directory">Working directory: ${escapeHTML(prepared.cwd||'Client service working directory')}</p><pre class="docker-command">${escapeHTML(prepared.command)}</pre><button class="primary" id="confirm-docker-update">Run update</button>`);
    $('#confirm-docker-update').onclick=()=>startDockerJob(path+'/update',{confirmation:prepared.id},true);
  }catch(error){if(generation===state.generation)dialogError(error);}
}
async function startDockerJob(path,payload,popup=false){
  if(dockerBusy())return;
  const generation=state.generation;state.starting=true;renderActions();
  if(popup)$('#confirm-docker-update').disabled=true;
  try{
    const job=await api(path,'POST',payload);
    if(generation!==state.generation)return;
    state.job=job;state.quickCleared=false;dockerState.activeJob=job.id;renderJob();if(!popup)setConsoleTab('quick');
    if(popup){
      dockerState.dialogJob=job.id;
      $('#docker-dialog-body').innerHTML='<p id="docker-progress-status" role="status"></p><pre id="docker-progress-output"></pre><p class="muted">View this run result in Recent actions. Container health appears in the Docker table.</p>';
      renderDockerProgress();
    }
    const jobs=await api(remotePath('jobs'));if(generation===state.generation)renderHistory(jobs);
  }catch(error){if(generation===state.generation){if(popup){dialogError(error);const button=$('#confirm-docker-update');if(button)button.disabled=false;}else fail(error);}}
  finally{if(generation===state.generation){state.starting=false;renderActions();}}
}
function renderDockerProgress(){
  if(state.job?.status!=='running')dockerState.activeJob=null;
  if(dockerState.dialogJob!==state.job?.id||!$('#docker-progress-output'))return;
  $('#docker-progress-output').textContent=state.job.output||'Waiting for command output…';
  $('#docker-progress-status').textContent=state.job.status==='succeeded'?'Update command completed.':state.job.status==='running'?'Update command running…':'Update command '+state.job.status.replace('_',' ')+'.';
}
$('#docker-rows').onclick=event=>{
  const button=event.target.closest('button');if(!button||button.disabled)return;
  if(button.classList.contains('state-dot')){button.focus();return;}
  const row=dockerState.rows.find(row=>row.id===button.dataset.container);if(!row)return;
  const operation=button.dataset.dockerOperation;
  if(operation==='edit')editDocker(row);
  else if(operation==='update')prepareDockerUpdate(row);
  else startDockerJob(remotePath('docker/'+row.id+'/control'),{operation});
};

load().catch(fail);
