const {test} = require('node:test');
const assert = require('node:assert/strict');
const {application,jsonReply,deferred,tick} = require('./app-host.cjs');
const owner = {role:'owner',can_write:true};

test('navigation immediately selects the target and shows loading until state and page both arrive', async () => {
  const app = await application(owner);
  const state = deferred(),posts = deferred();
  app.setResponder(url => url.includes('/state') ? state.promise : posts.promise);
  const pending = app.run('navigate("content")');
  const main = app.nodes.get('#main');
  assert.equal(main.dataset.screen,'loading');
  assert.equal(main.dataset.requestedScreen,'content');
  assert.equal(main.dataset.business,'');
  assert.equal(main.getAttribute('aria-busy'),'true');
  assert.equal(app.nodes.get('nav-content').getAttribute('aria-current'),'page');
  assert.equal(app.nodes.get('nav-overview').getAttribute('aria-current'),null);
  assert.equal(app.context.location.hash,'#content');
  assert.match(main.innerHTML,/Загружаем раздел/);
  assert.doesNotMatch(main.innerHTML,/workspace-home/);
  state.resolve(jsonReply(app.data)); await tick();
  assert.equal(main.dataset.screen,'loading','The page response is still pending');
  posts.resolve(jsonReply({items:[],more:false,total:0})); await pending;
  assert.equal(main.dataset.screen,'content');
  assert.equal(main.dataset.business,'one');
  assert.equal(main.getAttribute('aria-busy'),'false');
  assert.equal(app.requests.length,3);
  assert.ok(app.requests.every(({options}) => options.method==='GET'));
});

test('failure persists in the selected section and reviewer can explicitly retry a GET', async () => {
  const app = await application();
  app.setResponder(async () => { throw new Error('Нет связи с сервером'); });
  await assert.rejects(app.run('navigate("content")'),/Нет связи/);
  const main = app.nodes.get('#main');
  assert.equal(main.dataset.screen,'error');
  assert.equal(main.dataset.requestedScreen,'content');
  assert.equal(main.dataset.business,'');
  assert.equal(main.getAttribute('aria-busy'),'false');
  assert.match(main.innerHTML,/role="alert"/);
  assert.match(main.innerHTML,/\/menu/);
  assert.match(main.innerHTML,/Повторить загрузку/);
  assert.doesNotMatch(main.innerHTML,/workspace-home/);
  await tick();
  assert.equal(app.requests.length,2,'No automatic retry');
  app.setResponder(app.defaultResponder);
  const button = {disabled:false,dataset:{action:'refresh'}};
  await app.listeners.get('document:click')({target:{closest:()=>button},preventDefault() {}});
  assert.equal(main.dataset.screen,'content');
  assert.equal(app.requests.length,4,'Explicit retry requests state and posts exactly once');
  assert.ok(app.requests.every(({options}) => options.method==='GET'));
});

test('late success and late failure from an earlier navigation cannot replace the latest page', async () => {
  for (const fail of [false,true]) {
    const app = await application(owner);
    const old = deferred();
    let states=0;
    app.setResponder(url => url.includes('/state') && ++states===1 ? old.promise : app.defaultResponder(url));
    const first = app.run('navigate("content")');
    await app.run('navigate("reports")');
    assert.equal(app.nodes.get('#main').dataset.screen,'reports');
    if (fail) old.reject(new Error('Obsolete network error'));
    else old.resolve(jsonReply(app.data));
    await first;
    assert.equal(app.nodes.get('#main').dataset.screen,'reports');
    assert.equal(app.nodes.get('nav-reports').getAttribute('aria-current'),'page');
    assert.doesNotMatch(app.nodes.get('#main').innerHTML,/Obsolete network error/);
  }
});

test('switching companies clears old content and a late old posts response cannot render', async () => {
  for (const fail of [false,true]) {
    const app = await application(owner,false,{configure:data => {
      data.businesses.push({id:'two',name:'Другая компания'});
    }});
    const oldPosts=deferred(),newState=deferred();
    const second = {...app.data,business:{...app.data.business,id:'two',name:'Другая компания'}};
    app.setResponder(url => {
      if (url.includes('/state?business=two')) return newState.promise;
      if (url.includes('/business/one/posts?')) return oldPosts.promise;
      return app.defaultResponder(url);
    });
    const first = app.run('navigate("content")'); await tick();
    app.nodes.get('#workspace').value='two';
    const switching = app.listeners.get('#workspace:change')();
    const main = app.nodes.get('#main');
    assert.equal(main.dataset.screen,'loading');
    assert.equal(main.dataset.business,'');
    assert.match(main.innerHTML,/Другая компания/);
    oldPosts.resolve(jsonReply({items:[],more:false,total:0})); await first;
    assert.equal(main.dataset.screen,'loading','An old page does not render while the new company is loading');
    assert.equal(app.nodes.get('#workspace').value,'two');
    if (fail) newState.reject(new Error('Сеть недоступна'));
    else newState.resolve(jsonReply(second));
    await switching;
    if (fail) {
      assert.equal(main.dataset.screen,'error');
      assert.equal(main.dataset.business,'');
      app.setResponder(url => url.includes('/state') ? jsonReply(second) : app.defaultResponder(url));
      const button = {disabled:false,dataset:{action:'refresh'}};
      await app.listeners.get('document:click')({target:{closest:()=>button},preventDefault() {}});
      assert.ok(app.requests.at(-2).url.endsWith('/state?business=two'),'Retry targets the newly selected company');
    }
    assert.equal(main.dataset.screen,'content');
    assert.equal(main.dataset.business,'two');
    assert.equal(app.nodes.get('#workspace').value,'two');
    assert.equal(app.run('state.business.id'),'two');
  }
});

test('malformed JSON or a wrong state shape produces a recoverable page instead of a blank screen', async () => {
  for (const response of [jsonReply(null),{ok:true,json:async()=>{throw new SyntaxError('Unexpected token <');}}]) {
    const app = await application(owner);
    app.setResponder(async () => response);
    await assert.rejects(app.run('navigate("work")'));
    assert.equal(app.nodes.get('#main').dataset.screen,'error');
    assert.match(app.nodes.get('#main').innerHTML,/Повторить загрузку/);
    assert.match(app.nodes.get('#main').innerHTML,/студи/);
    assert.doesNotMatch(app.nodes.get('#main').innerHTML,/Unexpected token/);
  }
});

test('home distinguishes AI from manual creation and explains the missing-channel next step', async () => {
  const app = await application(owner,false,{configure:data => {
    data.business.channel_id=null;
    data.sources=[{id:'source',name:'Описание',kind:'text'}];
  }});
  const main = app.nodes.get('#main');
  assert.match(main.innerHTML,/data-action="new-post"[^>]*>.*?Сгенерировать с AI/);
  assert.match(main.innerHTML,/data-action="manual-post"[^>]*>.*?Создать вручную/);
  assert.match(main.innerHTML,/Следующий шаг: канал/);
  assert.match(main.innerHTML,/В AI-диалоге уже можно задавать вопросы/);
  assert.match(main.innerHTML,/data-action="channel"/);
  assert.doesNotMatch(main.innerHTML,/data-action="confirm-profile"/,'A completed setup step is not a new mutation');
});
