const {chromium}=require(process.env.ORCA_PLAYWRIGHT || 'playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.ORCA_BROWSER || undefined});
 const page=await browser.newPage({viewport:{width:1280,height:1000}});
 const errors=[];page.on('pageerror',error=>errors.push(error.message));
 await page.goto('http://127.0.0.1:'+(process.env.ORCA_PREVIEW_PORT || '8765'));
 await page.getByRole('button',{name:'Admin login',exact:true}).click();
 await page.getByLabel('Password',{exact:true}).fill('orca-preview-local');
 await page.getByRole('button',{name:'Sign in',exact:true}).click();
 await page.locator('.server').first().waitFor();
 async function api(path,body,method=body?'POST':'GET'){return page.evaluate(async({path,body,method})=>{
   const session=await (await fetch('/api/session')).json();
   const response=await fetch(path,{method,headers:{'Content-Type':'application/json','X-CSRF-Token':session.csrf},body:body?JSON.stringify(body):undefined});
   if(!response.ok)throw Error(await response.text());return response.json();
 },{path,body,method});}
 for(const base of ['/api/clients','/api/shortcuts']){
   for(const item of await api(base))if(item.name.startsWith('Drag '))await api(base+'/'+item.id,undefined,'DELETE');
 }
 const clients=await api('/api/clients');const original=clients.find(item=>item.name==='Local test client');
 for(const name of ['Drag client A','Drag client B'])await api('/api/clients',{name,url:'http://127.0.0.1:1',token:'x'.repeat(48)});
 for(const name of ['Drag link A','Drag link B'])await api('/api/shortcuts',{name,url:'https://example.com/'+name.replaceAll(' ','-'),visibility:'shared'});
 const actionsBase='/api/clients/'+original.id+'/remote/actions';
 for(const item of await api(actionsBase))if(item.name.startsWith('Drag '))await api(actionsBase+'/'+item.id,undefined,'DELETE');
 for(const name of ['Drag action A','Drag action B'])await api(actionsBase,{name,command:'printf harmless'});
 await page.reload();await page.locator('.server').first().waitFor();
 async function ids(selector){return page.locator(selector+' > [data-item-id]').evaluateAll(items=>items.map(item=>item.dataset.itemId));}
 async function mouseMove(selector){
   const before=await ids(selector);assert.ok(before.length>=2);
   const first=page.locator(selector+' > [data-item-id]').first();
   await first.scrollIntoViewIfNeeded();
   const handle=await first.locator('.drag-handle').boundingBox();
   const target=await page.locator(selector+' > [data-item-id]').nth(1).boundingBox();
   const saved=page.waitForResponse(r=>r.url().endsWith('/order') && r.request().method()==='PUT');
   await page.mouse.move(handle.x+handle.width/2,handle.y+handle.height/2);
   await page.mouse.down();
   await page.mouse.move(target.x+target.width/2,target.y+target.height/2,{steps:10});
   await page.mouse.up();assert.equal((await saved).status(),200);
   const expected=[before[1],before[0],...before.slice(2)];
   await page.waitForFunction(({selector,expected})=>JSON.stringify([...document.querySelector(selector).children].map(item=>item.dataset.itemId))===JSON.stringify(expected),{selector,expected});
   assert.deepEqual(await ids(selector),expected);return expected;
 }
 const clientOrder=await mouseMove('#servers');
 const shortcutOrder=await mouseMove('#shortcuts');
 await page.locator('[data-select="'+original.id+'"]').click();
 await page.getByRole('button',{name:'Drag action A',exact:true}).waitFor();
 const actionOrder=await mouseMove('#actions');
 assert.equal(await page.locator('#output').textContent(),'Run an action to view its output.');
 await page.reload();await page.locator('.server').first().waitFor();
 assert.deepEqual(await ids('#servers'),clientOrder);assert.deepEqual(await ids('#shortcuts'),shortcutOrder);
 await page.locator('[data-select="'+original.id+'"]').click();
 await page.getByRole('button',{name:'Drag action A',exact:true}).waitFor();
 assert.deepEqual(await ids('#actions'),actionOrder);
 // Editing retains the saved order.
 await page.getByRole('button',{name:'Edit Drag action A',exact:true}).click();
 await page.getByRole('button',{name:'Save action',exact:true}).click();
 await page.getByRole('button',{name:'Drag action A',exact:true}).waitFor();
 assert.deepEqual(await ids('#actions'),actionOrder);
 await page.route('**/api/clients/order',route=>route.fulfill({status:500,contentType:'application/json',body:JSON.stringify({error:'Test save failure'})}));
 await page.locator('#servers .drag-handle').first().press('ArrowRight');
 await page.getByRole('alert').filter({hasText:'Test save failure'}).waitFor();
 assert.deepEqual(await ids('#servers'),clientOrder);
 await page.unroute('**/api/clients/order');
 const keyboardSaved=page.waitForResponse(r=>r.url().endsWith('/order') && r.request().method()==='PUT');
 await page.locator('#actions .drag-handle').first().press('ArrowRight');
 assert.equal((await keyboardSaved).status(),200);
 const keyboardOrder=[actionOrder[1],actionOrder[0],...actionOrder.slice(2)];
 await page.waitForFunction(expected=>JSON.stringify([...document.querySelector('#actions').children].map(item=>item.dataset.itemId))===JSON.stringify(expected),keyboardOrder);
 await page.setViewportSize({width:390,height:844});
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 // Real touch events on a mobile browser surface.
 const mobile=await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true,storageState:await page.context().storageState()});
 const touch=await mobile.newPage();await touch.goto(page.url());await touch.locator('.server').first().waitFor();
 await touch.locator('#shortcuts').scrollIntoViewIfNeeded();
 const separated=await touch.locator('.shortcut-shell').first().evaluate(card=>{
   const edit=card.querySelector('.edit-control').getBoundingClientRect();
   const drag=card.querySelector('.drag-handle').getBoundingClientRect();
   const cardBox=card.getBoundingClientRect();
   return drag.right<=edit.left && Math.abs((drag.top+drag.bottom)/2-(cardBox.top+cardBox.bottom)/2)<1;
 });assert.equal(separated,true);
 const beforeTouch=await touch.locator('#shortcuts > [data-item-id]').evaluateAll(items=>items.map(item=>item.dataset.itemId));
 const start=await touch.locator('#shortcuts .drag-handle').first().boundingBox();
 const end=await touch.locator('#shortcuts > [data-item-id]').nth(1).boundingBox();
 const cdp=await mobile.newCDPSession(touch);
 const touchSaved=touch.waitForResponse(r=>r.url().endsWith('/order') && r.request().method()==='PUT');
 const from={x:start.x+start.width/2,y:start.y+start.height/2};
 const to={x:end.x+end.width/2,y:end.y+end.height/2};
 await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[from]});
 for(let i=1;i<=10;i++)await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:from.x+(to.x-from.x)*i/10,y:from.y+(to.y-from.y)*i/10}]});
 await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
 assert.equal((await touchSaved).status(),200);
 await touch.reload();await touch.locator('#shortcuts .drag-handle').first().waitFor();
 assert.deepEqual(await touch.locator('#shortcuts > [data-item-id]').evaluateAll(items=>items.map(item=>item.dataset.itemId)),[beforeTouch[1],beforeTouch[0],...beforeTouch.slice(2)]);
 await touch.screenshot({path:'/tmp/orca-reorder-mobile.png',fullPage:true});
 await page.setViewportSize({width:1280,height:1000});
 await page.screenshot({path:'/tmp/orca-reorder-desktop.png',fullPage:true});
 await page.getByRole('button',{name:'Shared view',exact:true}).click();
 await page.getByRole('heading',{name:'Your home base'}).waitFor();assert.equal(await page.locator('.drag-handle:visible').count(),0);
 assert.deepEqual(errors,[]);await browser.close();
 console.log('Reordering passed: clients, actions, shortcuts, persistence, edits, keyboard, touch, and admin-only handles.');
})().catch(error=>{console.error(error);process.exit(1)});
