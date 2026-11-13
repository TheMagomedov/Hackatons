const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const {application,tick} = require('./app-host.cjs');
const source = fs.readFileSync(path.join(__dirname,'../../app/miniapp/static/session.js'),'utf8');
const raw = 'query_id=test+value&user=%7B%22id%22%3A42%7D&auth_date=123&hash='+'a'.repeat(64);
const hash = '#'+new URLSearchParams({tgWebAppData:raw,tgWebAppVersion:'8.0'});

function setup(options={}) {
  const events=new Map(),replacements=[],native=[];
  const window={};
  if (options.sdk) window.Telegram={WebApp:options.sdk};
  if (options.native) window.TelegramWebviewProxy={postEvent:(...args)=>native.push(args)};
  if (options.external) window.external={notify:message=>native.push(message)};
  if (options.iframe) window.parent={postMessage:(...args)=>native.push(args)};
  Object.defineProperty(window,'localStorage',{get() {throw Error('Do not access browser storage');}});
  Object.defineProperty(window,'sessionStorage',{get() {throw Error('Do not access browser storage');}});
  const context=vm.createContext({window,URLSearchParams,setTimeout,clearTimeout,
    location:{hash:options.hash??hash,reload() {}},history:{replaceState:(...args)=>replacements.push(args)},
    document:{body:{dataset:{mode:options.preview?'preview':'live'}},
      querySelector:selector=>selector==='#telegram-sdk'?{addEventListener:(name,fn)=>events.set(name,fn)}:null},
  });
  vm.runInContext(source,context);
  return {session:window.TirazhSession,context,events,replacements,native,window};
}

test('launch fragment is decoded exactly once and forwarded as raw signed data without fabricating Telegram',async()=>{
  const app=setup();
  assert.equal(app.session.initData(),raw);
  assert.equal(await app.session.waitForInitData(),raw);
  assert.equal(app.window.Telegram,undefined);
  app.session.navigateHash('content');
  assert.equal(app.context.location.hash,hash,'Keep launch parameters for a late SDK and reload');
  assert.equal(app.replacements.length,0);
  const routed=setup({hash:'#content'}); routed.session.navigateHash('reports');
  assert.equal(routed.replacements[0][2],'#reports');
});

test('preview ignores launch credentials; ambiguous, missing or oversized fragment values do not enable entry',()=>{
  for (const options of [
    {preview:true},{hash:''},{hash:'#content'},
    {hash:'#tgWebAppData=first&tgWebAppData=second'},
    {hash:'#tgWebAppData='+encodeURIComponent('x'.repeat(8193))},
  ]) {
    const app=setup(options);
    assert.equal(app.session.initData(),'');
    app.session.ready();
    assert.equal(app.native.length,0);
  }
});

test('native fallback sends only ready through an existing bridge and uses a strict Telegram iframe target',()=>{
  const native=setup({native:true}); native.session.ready();
  assert.deepEqual(native.native,[['web_app_ready','""']]);
  assert.equal(native.window.Telegram,undefined);
  const external=setup({external:true}); external.session.ready();
  assert.deepEqual(JSON.parse(external.native[0]),{eventType:'web_app_ready',eventData:''});
  const iframe=setup({iframe:true}); iframe.session.ready();
  assert.deepEqual(JSON.parse(iframe.native[0][0]),{eventType:'web_app_ready',eventData:''});
  assert.equal(iframe.native[0][1],'https://web.telegram.org');
  assert.equal(JSON.stringify(iframe.native).includes('query_id'),false);
});

test('SDK wait is bounded, reports load failure, and accepts late genuine SDK data',async()=>{
  const stalled=setup({hash:''});
  assert.equal(await stalled.session.waitForInitData(1),'');
  const failed=setup({hash:''});
  const failure=failed.session.waitForInitData(); failed.events.get('error')();
  assert.equal(await failure,'');
  const late=setup({hash:''}); let ready=0,loaded=0;
  late.session.onBridge(()=>loaded++);
  const pending=late.session.waitForInitData();
  const bridge={initData:raw,ready:()=>ready++};
  late.window.Telegram={WebApp:bridge}; late.events.get('load')();
  assert.equal(await pending,raw); assert.equal(loaded,1);
  late.session.ready(); assert.equal(ready,1); assert.equal(late.session.sdk(),bridge);
});

test('real app paints and loads API with launch data while SDK is blocked; backend still decides authorization',async()=>{
  const events=[];
  const app=await application({role:'reviewer',can_write:false},false,{sdk:false,hash,nativeBridge:{postEvent:(...args)=>events.push(args)}});
  assert.equal(app.nodes.get('#main').dataset.screen,'overview');
  assert.equal(app.context.window.Telegram,undefined);
  assert.equal(app.requests[0].options.headers['X-Telegram-Init-Data'],raw);
  assert.deepEqual(events,[['web_app_ready','""']]);
  await app.run('navigate("content")');
  assert.equal(app.context.location.hash,hash);
  assert.ok(app.requests.every(({options})=>options.headers['X-Telegram-Init-Data']===raw));
  const denied=await application(undefined,false,{sdk:false,hash,respond:()=>({ok:false,json:async()=>({error:'Доступ запрещён'})})});
  assert.equal(denied.nodes.get('#main').dataset.screen,'error');
  assert.match(denied.nodes.get('#main').innerHTML,/Доступ запрещён/);
  assert.equal(denied.run('canWrite()'),false);
});

test('without Telegram entry data the app stops waiting visibly and never authenticates from a user ID',async()=>{
  const app=await application(undefined,false,{sdk:false,hash:'#user_id=42'});
  app.fireTimers(); await tick();
  assert.equal(app.requests.length,0);
  assert.equal(app.nodes.get('#main').dataset.screen,'error');
  assert.match(app.nodes.get('#main').innerHTML,/Не получены данные входа Telegram/);
  assert.match(app.nodes.get('#main').innerHTML,/Повторить загрузку/);
  assert.equal(app.context.window.Telegram,undefined);
});

test('late SDK attaches ready, theme and BackButton once without repeating an API request',async()=>{
  const app=await application({role:'reviewer',can_write:false},false,{sdk:false,hash});
  await app.run('navigate("content")');
  const before=app.requests.length;
  const calls={ready:0,expand:0,back:0,show:0,theme:0};
  app.context.window.Telegram={WebApp:{
    initData:raw,colorScheme:'dark',
    ready(){calls.ready++;},expand(){calls.expand++;},isVersionAtLeast(){return true;},
    onEvent(name){assert.equal(name,'themeChanged');calls.theme++;},
    setHeaderColor(){},setBackgroundColor(){},
    BackButton:{onClick(){calls.back++;},show(){calls.show++;},hide(){}},
  }};
  app.listeners.get('#telegram-sdk:load')(); await tick();
  assert.equal(app.nodes.get('body').dataset.theme,'dark');
  assert.deepEqual(calls,{ready:1,expand:1,back:1,show:1,theme:1});
  assert.equal(app.requests.length,before,'Late SDK must not replay initial requests or mutations');
  app.listeners.get('#telegram-sdk:load')(); await tick();
  assert.deepEqual(calls,{ready:1,expand:1,back:1,show:1,theme:1},'Repeated bridge signals do not duplicate handlers');
  assert.equal(app.requests.length,before);
});
