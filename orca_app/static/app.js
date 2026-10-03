'use strict';
const $ = selector => document.querySelector(selector);
const state = {session:null, preview:false, clients:[], shortcuts:[], metrics:new Map(), selected:null, actions:[], job:null, historyJob:null, historyId:'', historyRequest:0, starting:false, quickCleared:false, generation:0, polling:false};
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pencilIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4l11-11-4-4L4 16v4Zm13.5-16.5 3 3-1.5 1.5-3-3 1.5-1.5Z"/></svg>';
const infoIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 10h2v7h-2v-7Zm0-3h2v2h-2V7Zm1-5a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm0 18a8 8 0 1 1 0-16 8 8 0 0 1 0 16Z"/></svg>';
const show = (selector, visible) => {$(selector).hidden = !visible;};
const isAdmin = () => state.session?.admin && !state.preview;
const isFleet = () => state.session?.role === 'orchestrator';
function fail(error) { $('#error').textContent = error.message || String(error); show('#error', true); }
function clearError() { show('#error', false); }
async function api(path, method='GET', data) {
  const response = await fetch(path, {method, credentials:'same-origin', headers:{'Content-Type':'application/json','X-CSRF-Token':state.session?.csrf || ''}, body:data === undefined ? undefined : JSON.stringify(data)});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'Request failed.');
  return value;
}
function remotePath(endpoint) { return isFleet() ? '/api/clients/'+state.selected+'/remote/'+endpoint : '/api/host/'+endpoint; }
function hideEditors() { ['client','shortcut','action'].forEach(k=>show('#'+k+'-editor',false)); show('#action-confirm',false); }
function setConsoleTab(name) {
  const quick=name==='quick';
  $('#quick-tab').classList.toggle('active',quick);
  $('#quick-tab').setAttribute('aria-selected',String(quick));
  $('#recent-tab').classList.toggle('active',!quick);
  $('#recent-tab').setAttribute('aria-selected',String(!quick));
  show('#quick-panel',quick);show('#recent-panel',!quick);
}
async function load() {
  state.session = await api('/api/session');
  if (!state.session.admin) state.preview = false;
  hideEditors();
  $('#page-title').textContent = isAdmin() ? (isFleet()?state.session.admin_title:state.session.client_name) : state.session.public_title;
  document.title = $('#page-title').textContent+' · Orca';
  $('#auth-button').textContent = state.session.admin ? 'Sign out' : 'Admin login';
  $('#preview').textContent = state.preview ? 'Return to admin' : 'Shared view';
  show('#preview', state.session.admin && isFleet());
  show('#add-client',isAdmin() && isFleet());
  show('#fleet',isAdmin() && isFleet());
  show('#fleet-summary',isAdmin() && isFleet());
  show('#shortcut-section',isFleet());
  show('#add-shortcut',isAdmin() && isFleet());
  show('#workspace',false);
  show('#open-workspace',isAdmin() && !isFleet() && !state.selected);
  if (isFleet()) {
    state.shortcuts = await api('/api/shortcuts'); renderShortcuts();
    if (isAdmin()) {
      state.clients = await api('/api/clients');
      if (!state.clients.some(c=>c.id===state.selected)) state.selected=null;
      renderClients();
      await refreshMetrics();
    }
  } else if (isAdmin()) { await refreshMetrics(); }
  if (isAdmin() && state.selected) await selectClient(state.selected);
}
function renderClients() {
  const online=state.clients.filter(c=>state.metrics.get(c.id)?.ok).length;
  $('#fleet-summary').textContent = `${state.clients.length} clients · ${online} online`;
  $('#servers').innerHTML = state.clients.length ? state.clients.map(c=>{
    const m=state.metrics.get(c.id), d=m?.data;
    const disk=d?.disks?.length?Math.max(...d.disks.map(v=>v.percent)):null;
    const meters=[['CPU',d?.cpu],['RAM',d?.ram],['Disk (fullest)',disk]];
    return `<div class="server-shell"><button class="server ${c.id===state.selected?'selected':''}" data-select="${c.id}" aria-pressed="${c.id===state.selected}"><div class="row"><strong>${escapeHTML(c.name)}</strong><span class="${m?.ok?'status':'offline'}">${m?.ok?'● Online':m?'○ Unavailable':'Connecting'}</span></div><p class="muted">${escapeHTML(c.url)}${d?' · '+escapeHTML(d.os):''}</p><div class="meters">${meters.map(([label,value])=>`<div class="meter">${label}<b>${m?.ok && value != null?escapeHTML(value)+'%':'—'}</b></div>`).join('')}</div></button><button class="icon-button edit-control" data-edit-client="${c.id}" aria-label="Edit ${escapeHTML(c.name)}">${pencilIcon}</button></div>`;
  }).join('') : '<p class="empty">Add your first client to manage its actions and see system metrics.</p>';
}
function renderShortcuts() {
  const items=state.shortcuts.filter(s=>isAdmin()||s.visibility==='shared');
  $('#shortcuts').innerHTML=items.length?items.map(s=>`<div class="shortcut-shell"><a class="shortcut" aria-label="${escapeHTML(s.name)}" href="${escapeHTML(s.url)}" target="${s.open_in==='same_tab'?'_self':'_blank'}" rel="noopener noreferrer"><span class="shortcut-icon" aria-hidden="true"><span>↗</span><img src="${escapeHTML(new URL('/favicon.ico',s.url).href)}" alt="" referrerpolicy="no-referrer"></span><div class="shortcut-copy"><h3>${escapeHTML(s.name)}${isAdmin()?` <span class="muted">- ${s.visibility==='shared'?'Shared':'Admin only'}</span>`:''}</h3><p class="muted shortcut-url" title="${escapeHTML(s.url)}">${escapeHTML(s.url)}</p></div></a>${isAdmin()?`<button class="icon-button edit-control" data-edit-shortcut="${s.id}" aria-label="Edit ${escapeHTML(s.name)}">${pencilIcon}</button>`:''}</div>`).join(''):'<p class="empty">No shortcuts yet.</p>';
  $('#shortcuts').querySelectorAll('img').forEach(img=>{
    const update=()=>{img.hidden=!img.naturalWidth;img.previousElementSibling.hidden=!!img.naturalWidth;};
    img.onload=update;img.onerror=update;if(img.complete && img.naturalWidth)update();
  });
}
async function refreshMetrics() {
  if (!isAdmin()) return;
  const generation=state.generation;
  const wasUnavailable=state.metrics.get(state.selected)?.ok===false;
  const clients=isFleet()?state.clients:[{id:'local'}];
  await Promise.all(clients.map(async c=>{
    try { const d=await api(isFleet()?'/api/clients/'+c.id+'/remote/metrics':'/api/host/metrics'); if(generation===state.generation)state.metrics.set(c.id,{ok:true,data:d}); }
    catch(e) { if(generation===state.generation)state.metrics.set(c.id,{ok:false,error:e.message}); }
  }));
  if(generation!==state.generation||!isAdmin())return;
  if(isFleet())renderClients();
  renderConnection();
  if(wasUnavailable && state.metrics.get(state.selected)?.ok)await selectClient(state.selected);
}
function renderConnection() {
  if(!state.selected)return;
  const c=state.clients.find(c=>c.id===state.selected),m=state.metrics.get(state.selected);
  $('#client-name').textContent = isFleet() ? c?.name || '' : state.session.client_name;
  const d=m?.data;
  $('#client-address').textContent = [isFleet()?c?.url:null,d?.os].filter(Boolean).join(' · ');
  show('#work-grid',!!m?.ok);show('#unavailable',!m?.ok);
  $('#unavailable').textContent=m?.error||'Connecting to client…';
  if(!isFleet()&&d)$('#client-address').textContent+=` · CPU ${d.cpu}% · RAM ${d.ram}%`;
}
async function selectClient(id) {
  show('#open-workspace',false);
  state.quickCleared=false;
  state.selected=id;state.job=null;state.historyJob=null;state.historyId='';state.historyRequest++;state.starting=false;state.actions=[];state.generation++;renderActions();
  const generation=state.generation; hideEditors(); clearError();
  $('#output').textContent='Run an action to view its output.';
  $('#job-history').innerHTML='<option value="">No actions yet</option>';
  $('#job-detail').textContent='';show('#job-detail',false);show('#clear-output',false);
  $('#history-output').textContent='Select a run to view its output.';
  $('#history-detail').textContent='';show('#history-detail',false);
  if(isFleet())renderClients();renderConnection();show('#workspace',true);
  if(!state.metrics.get(id)?.ok)return;
  const [actions,jobs]=await Promise.all([api(remotePath('actions')),api(remotePath('jobs'))]);
  if(generation!==state.generation)return;
  state.actions=actions;renderActions();renderHistory(jobs);
  const running=jobs.find(j=>j.status==='running');
  if(running){
    const job=await api(remotePath('jobs/'+running.id));
    if(generation===state.generation){state.job=job;renderJob();renderActions();}
  }
}
function renderActions() {
  const running=state.starting || state.job?.status==='running';
  $('#actions').innerHTML=state.actions.length?state.actions.map(a=>`<div class="action-shell"><button class="action-run" data-run="${a.id}" ${running?'disabled':''}>${escapeHTML(a.name)}</button><button class="icon-button" data-info-action="${a.id}" aria-label="Information about ${escapeHTML(a.name)}">${infoIcon}</button><button class="icon-button" data-edit-action="${a.id}" aria-label="Edit ${escapeHTML(a.name)}">${pencilIcon}</button></div>`).join(''):'<p class="empty">Add a command to create your first action.</p>';
}
function renderHistory(jobs) {
  $('#job-history').innerHTML='<option value="">'+(jobs.length?'Select a run':'No actions yet')+'</option>'+jobs.map(j=>`<option value="${j.id}">${escapeHTML(j.name)} · ${escapeHTML(j.status)} · ${escapeHTML(new Date(j.started*1000).toLocaleString())}</option>`).join('');
  $('#job-history').value=state.historyId;
}
async function selectJob(id) {
  const generation=state.generation,request=++state.historyRequest;
  state.historyId=id;state.historyJob=null;
  $('#history-output').textContent=id?'Loading output…':'Select a run to view its output.';
  show('#history-detail',false);
  if(!id)return;
  try {
    const job=await api(remotePath('jobs/'+id));
    if(generation!==state.generation || request!==state.historyRequest)return;
    state.historyJob=job;renderJob(job,true);
  } catch(e) {
    if(generation===state.generation && request===state.historyRequest)$('#history-output').textContent=e.message;
  }
}
function renderJob(j=state.job,history=false) {
  if(!j || (!history && state.quickCleared))return;
  $(history?'#history-output':'#output').textContent=j.output || (j.status==='running'?'Waiting for command output…':'No output.');
  const detail=history?'#history-detail':'#job-detail';
  $(detail).textContent=j.name+' · '+j.status.replace('_',' ')+(j.exit_code===null?'':` · Exit ${j.exit_code}`);
  show(detail,true);
  if(!history)show('#clear-output',true);
}
function input(label,name,value='',type='text',required=true) {
  return `<label>${label}<input name="${name}" type="${type}" value="${escapeHTML(value)}" ${required?'required':''} autocomplete="${type==='password'?'off':'on'}"></label>`;
}
function edit(kind,item=null) {
  if(kind==='client' && state.selected)closeWorkspace();
  if(kind==='action')show('#action-confirm',false);
  clearError();const box=$('#'+kind+'-editor');
  if(!box.hidden && box.dataset.item===(item?.id || 'new'))return;
  box.dataset.item=item?.id || 'new';
  let fields='';
  if(kind==='client')fields=input('Name','name',item?.name)+input('Client address (include port)','url',item?.url||'http://','url')+input(item?'Client token (blank keeps current)':'Client token','token','','password',!item);
  if(kind==='shortcut')fields=input('Name','name',item?.name)+input('URL','url',item?.url||'https://','url')+`<label>Visibility<select name="visibility"><option value="admin" ${item?.visibility!=='shared'?'selected':''}>Admin only</option><option value="shared" ${item?.visibility==='shared'?'selected':''}>Shared</option></select></label><label>Open in<select name="open_in"><option value="new_tab" ${item?.open_in!=='same_tab'?'selected':''}>New tab</option><option value="same_tab" ${item?.open_in==='same_tab'?'selected':''}>Same tab</option></select></label>`;
  if(kind==='action')fields=input('Name','name',item?.name)+input('Working directory (optional)','cwd',item?.cwd||'','text',false)+`<label class="full">Command<textarea name="command" rows="3" required>${escapeHTML(item?.command||'')}</textarea></label>`+input('Timeout (seconds)','timeout',item?.timeout||3600,'number');
  box.innerHTML=`<h2>${item?'Edit':'Add'} ${kind}</h2><form><div class="fields">${fields}</div><div class="row"><button class="primary">Save ${kind}</button><button type="button" data-close="${kind}-editor">Cancel</button>${item?'<div class="grow"></div><button type="button" class="danger" data-delete>Delete</button>':''}</div></form>`;
  box.hidden=false;box.querySelector('input').focus();
  const selected=state.selected;
  const base=kind==='client'?'/api/clients':kind==='shortcut'?'/api/shortcuts':remotePath('actions');
  box.querySelector('form').onsubmit=async event=>{
    event.preventDefault();const button=event.submitter;button.disabled=true;
    try {
      const data=Object.fromEntries(new FormData(event.target));if(kind==='action')data.timeout=Number(data.timeout);
      await api(base+(item?'/'+item.id:''),item?'PUT':'POST',data);box.hidden=true;
      if(kind==='action'&&selected===state.selected){state.actions=await api(remotePath('actions'));renderActions();}
      else if(kind==='shortcut'){state.shortcuts=await api('/api/shortcuts');renderShortcuts();}
      else await load();
    } catch(e){fail(e);} finally{button.disabled=false;}
  };
  if(item)box.querySelector('[data-delete]').onclick=async()=>{
    if(!confirm(`Delete ${kind} “${item.name}”?${kind==='client'?' This only removes it from Orca.':''}`))return;
    try{await api(base+'/'+item.id,'DELETE');box.hidden=true;if(kind==='action'){state.actions=await api(remotePath('actions'));renderActions();}else await load();}catch(e){fail(e);}
  };
}
function showActionInfo(id) {
  const a=state.actions.find(a=>a.id===id);if(!a)return;
  show('#action-editor',false);
  const box=$('#action-confirm');box.hidden=false;
  box.innerHTML=`<div class="row"><h3>${escapeHTML(a.name)}</h3><div class="grow"></div><button class="icon-button" data-close="action-confirm" aria-label="Close action details">X</button></div><pre class="action-command">${escapeHTML(a.command)}</pre><dl class="action-metadata"><div><dt>Working directory</dt><dd>${escapeHTML(a.cwd||'Client service working directory')}</dd></div><div><dt>Timeout</dt><dd>Timeout: ${a.timeout}s</dd></div></dl>`;
}
async function runAction(id) {
  if(state.starting || state.job?.status==='running')return;
  const a=state.actions.find(a=>a.id===id);if(!a)return;
  const generation=state.generation,path=remotePath('actions/'+id+'/run');
  clearError();
  state.starting=true;renderActions();
  try{
    const job=await api(path,'POST',{});if(generation!==state.generation)return;
    state.quickCleared=false;state.job=job;show('#action-confirm',false);setConsoleTab('quick');renderJob();renderActions();
    const jobs=await api(remotePath('jobs'));if(generation!==state.generation)return;
    renderHistory(jobs);renderJob();
  } catch(e){if(generation===state.generation)fail(e);}
  finally{if(generation===state.generation){state.starting=false;renderActions();}}
}
document.addEventListener('click',event=>{
  const b=event.target.closest('button');if(!b)return;
  if(b.dataset.close)show('#'+b.dataset.close,false);
  if(b.dataset.select)selectClient(b.dataset.select).catch(fail);
  for(const kind of ['client','shortcut','action']){
    const id=b.dataset['edit'+kind[0].toUpperCase()+kind.slice(1)];
    if(id)edit(kind,state[kind==='client'?'clients':kind==='shortcut'?'shortcuts':'actions'].find(i=>i.id===id));
  }
  if(b.dataset.infoAction)showActionInfo(b.dataset.infoAction);
  if(b.dataset.run)runAction(b.dataset.run);
});
$('#add-client').onclick=()=>edit('client');$('#add-shortcut').onclick=()=>edit('shortcut');$('#add-action').onclick=()=>edit('action');
$('#auth-button').onclick=async()=>{
  if(!state.session?.admin){show('#login',true);$('#login input').focus();return;}
  try{await api('/api/logout','POST',{});state.generation++;state.clients=[];state.metrics.clear();state.selected=null;state.job=null;state.actions=[];await load();}catch(e){fail(e);}
};
$('#login-form').onsubmit=async event=>{
  event.preventDefault();const button=event.submitter;button.disabled=true;clearError();
  try{state.session=await api('/api/login','POST',Object.fromEntries(new FormData(event.target)));event.target.reset();show('#login',false);await load();}catch(e){fail(e);}finally{button.disabled=false;}
};
$('#preview').onclick=()=>{state.preview=!state.preview;state.generation++;clearError();load().catch(fail);};
$('#clear-output').onclick=()=>{
  state.quickCleared=true;
  $('#output').textContent='Run an action to view its output.';
  $('#job-detail').textContent='';show('#job-detail',false);show('#clear-output',false);
};
$('#quick-tab').onclick=()=>setConsoleTab('quick');
$('#recent-tab').onclick=()=>setConsoleTab('recent');
$('#job-history').onchange=event=>selectJob(event.target.value);
let ticks=0;
setInterval(async()=>{
  if(state.polling||!isAdmin()||document.hidden)return;
  state.polling=true;const generation=state.generation;
  try{
    if(state.job?.status==='running'){
      const job=await api(remotePath('jobs/'+state.job.id));
      if(generation===state.generation){state.job=job;renderJob();if(job.status!=='running'){renderActions();const jobs=await api(remotePath('jobs'));if(generation===state.generation)renderHistory(jobs);}}
    }
    if(generation===state.generation && state.historyJob?.status==='running'){
      const request=state.historyRequest,id=state.historyId;
      const job=state.job?.id===id?state.job:await api(remotePath('jobs/'+id));
      if(generation===state.generation && request===state.historyRequest){state.historyJob=job;renderJob(job,true);}
    }
    if(++ticks%5===0)await refreshMetrics();
  }catch(e){fail(e);}finally{state.polling=false;}
},1000);
load().catch(fail);

function closeWorkspace() {
  state.selected=null;state.generation++;state.historyRequest++;
  state.job=null;state.historyJob=null;state.historyId='';state.actions=[];state.starting=false;
  show('#action-editor',false);show('#action-confirm',false);show('#workspace',false);
  if(isFleet())renderClients();
  else show('#open-workspace',true);
}
$('#workspace-close').onclick=closeWorkspace;
$('#open-workspace').onclick=async()=>{
  try{await refreshMetrics();await selectClient('local');}catch(e){fail(e);}
};
