'use strict';
const $ = selector => document.querySelector(selector);
const state = {session:null, preview:false, clients:[], shortcuts:[], metrics:new Map(), selected:null, actions:[], job:null, generation:0, polling:false};
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
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
function notice(message) { $('#notice').textContent = message; }
function hideEditors() { ['client','shortcut','action'].forEach(k=>show('#'+k+'-editor',false)); show('#action-confirm',false); }
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
  if (isFleet()) {
    state.shortcuts = await api('/api/shortcuts'); renderShortcuts();
    if (isAdmin()) {
      state.clients = await api('/api/clients');
      if (!state.clients.some(c=>c.id===state.selected)) state.selected=state.clients[0]?.id || null;
      renderClients();
      await refreshMetrics();
    }
  } else if (isAdmin()) { state.selected='local'; await refreshMetrics(); }
  if (isAdmin() && state.selected) await selectClient(state.selected);
}
function renderClients() {
  const online=state.clients.filter(c=>state.metrics.get(c.id)?.ok).length;
  $('#fleet-summary').textContent = `${state.clients.length} clients · ${online} online`;
  $('#servers').innerHTML = state.clients.length ? state.clients.map(c=>{
    const m=state.metrics.get(c.id), d=m?.data;
    const disk=d?.disks?.length?Math.max(...d.disks.map(v=>v.percent)):null;
    const meters=[['CPU',d?.cpu],['RAM',d?.ram],['Disk (fullest)',disk]];
    return `<div class="server-shell"><button class="server ${c.id===state.selected?'selected':''}" data-select="${c.id}" aria-pressed="${c.id===state.selected}"><div class="row"><strong>${escapeHTML(c.name)}</strong><div class="grow"></div><span class="${m?.ok?'status':'offline'}">${m?.ok?'● Online':m?'○ Unavailable':'Connecting'}</span></div><p class="muted">${escapeHTML(c.url)}${d?' · '+escapeHTML(d.os):''}</p><div class="meters">${meters.map(([label,value])=>`<div class="meter">${label}<b>${m?.ok && value != null?escapeHTML(value)+'%':'—'}</b></div>`).join('')}</div></button><button class="server-edit" data-edit-client="${c.id}">Edit client</button></div>`;
  }).join('') : '<p class="empty">Add your first client to manage its actions and see system metrics.</p>';
}
function renderShortcuts() {
  const items=state.shortcuts.filter(s=>isAdmin()||s.visibility==='shared');
  $('#shortcuts').innerHTML=items.length?items.map(s=>`<div class="shortcut-shell"><a class="shortcut" href="${escapeHTML(s.url)}" target="_blank" rel="noopener noreferrer"><div><h3>${escapeHTML(s.name)} ↗</h3>${isAdmin()?`<p class="muted">${s.visibility==='shared'?'Shared':'Admin only'}</p>`:''}</div></a>${isAdmin()?`<button class="server-edit" data-edit-shortcut="${s.id}">Edit shortcut</button>`:''}</div>`).join(''):'<p class="empty">No shortcuts yet.</p>';
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
  $('#client-address').textContent = [isFleet()?c?.url:null,d?.os,d?.execution_target,'Client manager'].filter(Boolean).join(' · ');
  $('#connection').textContent = m?.ok?'● Connected':'○ Unavailable';
  show('#work-grid',!!m?.ok);show('#unavailable',!m?.ok);
  $('#unavailable').textContent=m?.error||'Connecting to client…';
  if(d?.metrics_scope)$('#client-address').textContent+=' · '+d.metrics_scope;
  if(!isFleet()&&d)$('#client-address').textContent+=` · CPU ${d.cpu}% · RAM ${d.ram}%`;
}
async function selectClient(id) {
  state.selected=id;state.job=null;state.actions=[];state.generation++;renderActions();
  const generation=state.generation; hideEditors(); clearError();
  $('#output').textContent='Select an action to view its output.';$('#job-status').textContent='Ready';
  $('#job-history').innerHTML='<option value="">No actions yet</option>';
  $('#job-detail').textContent='Output is saved on this client.';
  if(isFleet())renderClients();renderConnection();show('#workspace',true);
  if(!state.metrics.get(id)?.ok)return;
  const [actions,jobs]=await Promise.all([api(remotePath('actions')),api(remotePath('jobs'))]);
  if(generation!==state.generation)return;
  state.actions=actions;renderActions();renderHistory(jobs);
  if(jobs.length)await selectJob(jobs[0].id);
}
function renderActions() {
  $('#actions').innerHTML=state.actions.length?state.actions.map(a=>`<div class="action-shell"><button data-run="${a.id}">${escapeHTML(a.name)}</button><button data-edit-action="${a.id}" aria-label="Edit ${escapeHTML(a.name)}">Edit</button></div>`).join(''):'<p class="empty">Add a command to create your first action.</p>';
}
function renderHistory(jobs) {
  $('#job-history').innerHTML=jobs.length?jobs.map(j=>`<option value="${j.id}">${escapeHTML(j.name)} · ${escapeHTML(j.status)} · ${escapeHTML(new Date(j.started*1000).toLocaleString())}</option>`).join(''):'<option value="">No actions yet</option>';
  if(state.job)$('#job-history').value=state.job.id;
}
async function selectJob(id) {
  const generation=state.generation;
  const job=await api(remotePath('jobs/'+id));
  if(generation!==state.generation)return;
  state.job=job;renderJob();
}
function renderJob() {
  const j=state.job;if(!j)return;
  $('#output').textContent=j.output || (j.status==='running'?'Waiting for command output…':'No output.');
  $('#job-status').textContent=j.status.replace('_',' ');
  $('#job-detail').textContent=j.name+' · '+(j.exit_code===null?'':`Exit ${j.exit_code} · `)+'Latest 256 KiB of output retained';
  $('#job-history').value=j.id;
}
function input(label,name,value='',type='text',required=true) {
  return `<label>${label}<input name="${name}" type="${type}" value="${escapeHTML(value)}" ${required?'required':''} autocomplete="${type==='password'?'off':'on'}"></label>`;
}
function edit(kind,item=null) {
  clearError();const box=$('#'+kind+'-editor');
  if(!box.hidden && box.dataset.item===(item?.id || 'new'))return;
  box.dataset.item=item?.id || 'new';
  let fields='';
  if(kind==='client')fields=input('Name','name',item?.name)+input('Client address (include port)','url',item?.url||'http://','url')+input(item?'Client token (blank keeps current)':'Client token','token','','password',!item);
  if(kind==='shortcut')fields=input('Name','name',item?.name)+input('URL','url',item?.url||'https://','url')+`<label>Visibility<select name="visibility"><option value="admin" ${item?.visibility!=='shared'?'selected':''}>Admin only</option><option value="shared" ${item?.visibility==='shared'?'selected':''}>Shared</option></select></label>`;
  if(kind==='action')fields=input('Name','name',item?.name)+input('Working directory (optional)','cwd',item?.cwd||'','text',false)+`<label class="full">Command<textarea name="command" rows="3" required>${escapeHTML(item?.command||'')}</textarea></label>`+input('Timeout (seconds)','timeout',item?.timeout||3600,'number');
  box.innerHTML=`<h2>${item?'Edit':'Add'} ${kind}</h2><form><div class="fields">${fields}</div><div class="row"><button class="primary">Save ${kind}</button><button type="button" data-close="${kind}-editor">Cancel</button>${item?'<div class="grow"></div><button type="button" class="danger" data-delete>Delete</button>':''}</div></form>`;
  box.hidden=false;box.querySelector('input').focus();
  const selected=state.selected;
  const base=kind==='client'?'/api/clients':kind==='shortcut'?'/api/shortcuts':remotePath('actions');
  box.querySelector('form').onsubmit=async event=>{
    event.preventDefault();const button=event.submitter;button.disabled=true;
    try {
      const data=Object.fromEntries(new FormData(event.target));if(kind==='action')data.timeout=Number(data.timeout);
      await api(base+(item?'/'+item.id:''),item?'PUT':'POST',data);box.hidden=true;notice(`${kind[0].toUpperCase()+kind.slice(1)} saved.`);
      if(kind==='action'&&selected===state.selected){state.actions=await api(remotePath('actions'));renderActions();}
      else if(kind==='shortcut'){state.shortcuts=await api('/api/shortcuts');renderShortcuts();}
      else await load();
    } catch(e){fail(e);} finally{button.disabled=false;}
  };
  if(item)box.querySelector('[data-delete]').onclick=async()=>{
    if(!confirm(`Delete ${kind} “${item.name}”?${kind==='client'?' This only removes it from Orca.':''}`))return;
    try{await api(base+'/'+item.id,'DELETE');box.hidden=true;if(kind==='action'){state.actions=await api(remotePath('actions'));renderActions();}else await load();notice('Deleted.');}catch(e){fail(e);}
  };
}
function confirmAction(id) {
  const a=state.actions.find(a=>a.id===id);if(!a)return;
  const box=$('#action-confirm');box.hidden=false;
  box.innerHTML=`<h3>Run ${escapeHTML(a.name)}?</h3><pre>${escapeHTML(a.command)}</pre><p class="muted">${escapeHTML(a.cwd||'Client service working directory')} · Timeout ${a.timeout}s</p><div class="row"><button class="primary" id="confirm-run">Run action</button><button data-close="action-confirm">Cancel</button></div>`;
  const generation=state.generation,path=remotePath('actions/'+id+'/run');
  $('#confirm-run').onclick=async()=>{
    $('#confirm-run').disabled=true;
    try{const job=await api(path,'POST',{});if(generation!==state.generation)return;state.job=job;box.hidden=true;const jobs=await api(remotePath('jobs'));renderHistory(jobs);renderJob();notice(a.name+' started.');}
    catch(e){fail(e);if($('#confirm-run'))$('#confirm-run').disabled=false;}
  };
}
document.addEventListener('click',event=>{
  const b=event.target.closest('button');if(!b)return;
  if(b.dataset.close)show('#'+b.dataset.close,false);
  if(b.dataset.select)selectClient(b.dataset.select).catch(fail);
  for(const kind of ['client','shortcut','action']){
    const id=b.dataset['edit'+kind[0].toUpperCase()+kind.slice(1)];
    if(id)edit(kind,state[kind==='client'?'clients':kind==='shortcut'?'shortcuts':'actions'].find(i=>i.id===id));
  }
  if(b.dataset.run)confirmAction(b.dataset.run);
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
$('#job-history').onchange=event=>{if(event.target.value)selectJob(event.target.value).catch(fail);};
let ticks=0;
setInterval(async()=>{
  if(state.polling||!isAdmin()||document.hidden)return;
  state.polling=true;const generation=state.generation;
  try{
    if(state.job?.status==='running'){
      const job=await api(remotePath('jobs/'+state.job.id));
      if(generation===state.generation){state.job=job;renderJob();if(job.status!=='running'){const jobs=await api(remotePath('jobs'));if(generation===state.generation)renderHistory(jobs);}}
    }
    if(++ticks%5===0)await refreshMetrics();
  }catch(e){fail(e);}finally{state.polling=false;}
},1000);
load().catch(fail);
