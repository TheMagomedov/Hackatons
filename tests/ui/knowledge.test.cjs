const assert = require('node:assert/strict');
const test = require('node:test');
const {application,jsonReply,deferred} = require('./app-host.cjs');
const owner = {role:'owner',can_write:true};

test('business sources explain documents and operator search; reviewer keeps read-only controls',async()=>{
  for (const access of [owner,{role:'reviewer',can_write:false}]) {
    const app=await application(access);
    await app.run('navigate("business")');
    const html=app.nodes.get('#main').innerHTML;
    assert.match(html,/договор, регламент или брендбук/);
    assert.equal(html.includes('data-action="knowledge-search"'),access.can_write);
  }
});

test('retrieved source text and identifiers are escaped, with explicit no-results and preview states',async()=>{
  const app=await application(owner);
  app.context.payload={items:[{source_name:'<img src=x onerror=attack()>',text:'<script>attack()</script>',
    source_id:'source&one',chunk_id:'chunk"one'}]};
  const html=app.run('knowledgeResults(payload)');
  assert.doesNotMatch(html,/<img|<script/);
  assert.match(html,/&lt;script&gt;/);
  assert.match(html,/source&amp;one/);
  assert.match(html,/chunk&quot;one/);
  assert.match(app.run('knowledgeResults({items:[]})'),/Подходящих фрагментов нет/);
  assert.match(app.run('knowledgeResults({items:[],preview:true})'),/В предпросмотре документы не индексируются/);
  assert.throws(()=>app.run('knowledgeResults({})'),/результаты поиска/);
});

function searchForm(app) {
  const submit={disabled:false,textContent:'Найти фрагменты'};
  const field={disabled:false};
  const error=app.run('$("#form-error")');
  const results={html:'',text:'',attrs:{},setAttribute(key,value){this.attrs[key]=value;},
    set innerHTML(value){this.html=value;this.text='';},get innerHTML(){return this.html;},
    set textContent(value){this.text=value;this.html='';},get textContent(){return this.text;}};
  const form={dataset:{form:'knowledge-search',id:'one'},isConnected:true,elements:[field,submit],
    values:[['query','Срок уведомления']],querySelector(selector){
      return selector==='[type="submit"]' ? submit : selector==='#form-error' ? error : results;
    }};
  app.context.FormData=function(value){return new Map(value.values);};
  app.nodes.get('#dialog').open=true;
  return {form,submit,field,results,event:{target:form,preventDefault(){}}};
}

test('document search submits once, preserves the selected company and renders source evidence',async()=>{
  const app=await application(owner);
  const gate=deferred(); app.setResponder(()=>gate.promise);
  const fixture=searchForm(app);
  const handler=app.listeners.get('#dialog:submit');
  const pending=handler(fixture.event);
  assert.equal(fixture.submit.disabled,true);
  assert.equal(fixture.results.attrs['aria-busy'],'true');
  await handler(fixture.event);
  const request=app.requests.at(-1);
  assert.equal(request.url,'./api/business/one/knowledge/search');
  assert.deepEqual(JSON.parse(request.options.body),{query:'Срок уведомления',limit:4});
  assert.equal(app.requests.filter(r=>r.options.method==='POST').length,1);
  gate.resolve(jsonReply({items:[{source_name:'Регламент',source_id:'s1',chunk_id:'s1:h:1',text:'За 37 дней.'}]}));
  await pending;
  assert.match(fixture.results.innerHTML,/37 дней/);
  assert.match(fixture.results.innerHTML,/s1:h:1/);
  assert.equal(fixture.results.attrs['aria-busy'],'false');
  assert.equal(fixture.submit.disabled,false);
});

test('late search result cannot overwrite a dialog after switching company',async()=>{
  const app=await application(owner);
  const gate=deferred();app.setResponder(()=>gate.promise);
  const fixture=searchForm(app);
  const pending=app.listeners.get('#dialog:submit')(fixture.event);
  app.run('businessId="two";dialog.close()');
  gate.resolve(jsonReply({items:[{source_name:'Old',source_id:'old',chunk_id:'old:1',text:'Old private result'}]}));
  await pending;
  assert.doesNotMatch(fixture.results.innerHTML,/Old private result/);
});

test('search failure is visible and does not automatically repeat the POST',async()=>{
  const app=await application(owner);
  app.setResponder(async()=>{throw new Error('Поиск временно недоступен');});
  const fixture=searchForm(app);
  await app.listeners.get('#dialog:submit')(fixture.event);
  assert.match(app.nodes.get('#form-error').textContent,/Поиск временно недоступен/);
  assert.equal(fixture.results.textContent,'');
  assert.equal(fixture.results.attrs['aria-busy'],'false');
  assert.equal(fixture.submit.disabled,false);
  assert.equal(app.requests.filter(r=>r.options.method==='POST').length,1);
});

test('collapsed diagnostics show metadata only, including cache/failure/time and escaped trace links',async()=>{
  const app=await application(owner);
  app.context.fixture={knowledge:{enabled:true,sources:2,chunks:7},traces:[
    {id:'trace"one',event:'cache_hit',kind:'interpret',model:'<script>model()</script>',duration_ms:25,
      created:1794576000,prompt:'PRIVATE_PROMPT_MUST_NOT_RENDER',response:'PRIVATE_RESPONSE'},
    {id:'trace-two',event:'model_failed',kind:'text',model:'local-qwen',duration_ms:1500,created:1794576001},
  ]};
  app.run('controls=fixture');
  const html=app.run('diagnosticsView()');
  assert.match(html,/<details[^>]*><summary>Диагностика/);
  assert.doesNotMatch(html,/<details[^>]*open|PRIVATE_|<script>/);
  assert.match(html,/Ответ из кеша/);
  assert.match(html,/Модель недоступна/);
  assert.match(html,/1.50 с/);
  assert.match(html,/href="#ai-trace-trace%22one"/);
  assert.match(html,/<a data-trace href="#ai-trace-/);
  assert.match(html,/&lt;script&gt;model/);
});

test('trace links focus their record without replacing signed launch data or making requests',async()=>{
  const launch='#'+new URLSearchParams({tgWebAppData:'signed-data',tgWebAppVersion:'8.0'});
  for (const hash of [launch,'#work']) {
    const app=await application({role:'reviewer',can_write:false},false,{sdk:false,hash:launch});
    app.context.location.hash=hash;
    const before=app.requests.length;
    const calls=[];
    const entry={setAttribute:(...args)=>calls.push(['attribute',...args]),
      focus:options=>calls.push(['focus',options.preventScroll]),
      scrollIntoView:options=>calls.push(['scroll',options.block])};
    const link={dataset:{trace:''},closest:selector=>selector==='li[id]'?entry:null};
    let prevented=false;
    const event={target:{closest:selector=>selector.includes('[data-trace]')?link:null},
      preventDefault(){prevented=true;}};
    await app.listeners.get('document:click')(event);
    assert.equal(prevented,true,'Stop the native fragment navigation for pointer and keyboard clicks');
    assert.equal(app.context.location.hash,hash);
    assert.deepEqual(calls,[['attribute','tabindex','-1'],['focus',true],['scroll','nearest']]);
    assert.equal(app.requests.length,before,'Diagnostics navigation is read-only and local');
  }
});
