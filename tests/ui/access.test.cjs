const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const {application} = require('./app-host.cjs');
const staticDir = path.join(__dirname, '../../app/miniapp/static');
const source = fs.readFileSync(path.join(staticDir, 'access.js'), 'utf8');
const reviewer = {role:'reviewer', can_write:false};
const owner = {role:'owner', can_write:true};

function rules() {
  const context = vm.createContext({window:{}});
  vm.runInContext(source, context);
  return context.window.TirazhAccess;
}
function element(attrs = {}, children = []) {
  return {
    dataset:Object.fromEntries(Object.entries(attrs).filter(([key]) => key.startsWith('data-'))
      .map(([key, value]) => [key.slice(5), value])),
    hidden:false, disabled:false, children,
    hasAttribute(name) { return Object.hasOwn(attrs, name); },
    setAttribute(name, value) { attrs[name] = value; },
    removeAttribute(name) { delete attrs[name]; },
    querySelectorAll() { return children; },
  };
}

test('only explicit owner/operator write grants are accepted; preview has a local default', () => {
  const access = rules();
  for (const input of [undefined, null, {}, {role:'owner'}, {role:'operator',can_write:'true'},
    reviewer, {role:'reviewer',can_write:true}, {role:'admin',can_write:true}]) {
    assert.equal(access.resolve(input).can_write, false);
  }
  assert.equal(access.resolve(owner).can_write, true);
  assert.equal(access.resolve({role:'operator',can_write:true}).can_write, true);
  assert.equal(access.resolve(undefined, true).can_write, true);
  assert.equal(access.resolve(reviewer, true).can_write, false);
});

test('reviewers retain reads and cannot invoke any mutation or future unclassified action', () => {
  const access = rules();
  for (const action of ['new-business','manual-profile','analyze','confirm-profile','add-source','upload-pdf',
    'channel','manual-post','new-post','edit-post','replace-image','revise-text','revise-image','refresh-context',
    'approve','reject','automation','toggle-pause','cancel-job','retry-notification','future-mutation']) {
    assert.equal(access.requiresWrite(action), true, action);
  }
  for (const action of ['close','theme','refresh','open-post','filter','more','clear-search','content-layout',
    'calendar-day','calendar-prev','calendar-next','calendar-today','retry-image','download-image']) {
    assert.equal(access.requiresWrite(action), false, action);
  }
  for (const method of ['POST','PUT','PATCH','DELETE','post']) assert.throws(() => access.assertRequest(reviewer,method), /Доступ для просмотра/);
  for (const method of ['GET','HEAD','OPTIONS','get']) assert.doesNotThrow(() => access.assertRequest(reviewer,method));
  assert.doesNotThrow(() => access.assertRequest(owner,'POST'));
});

test('DOM pass hides dynamic mutations and form fields while preserving read controls', () => {
  const access = rules();
  const edit = element({'data-action':'edit-post'});
  const approve = element({'data-action':'approve'}); approve.disabled = true;
  const image = element({'data-action':'retry-image'});
  const download = element({'data-action':'download-image'});
  const search = element({'data-search':''});
  const input = element();
  const submit = element();
  const chat = element({'data-chat':''}, [input,submit]);
  const form = element({'data-form':'pdf'}, [element()]);
  const nodes = [edit,approve,image,download,search,chat,form];
  const container = {querySelectorAll:() => nodes.flatMap(node => [node,...node.children])};
  access.apply(container,reviewer);
  for (const node of [edit,approve,chat,form]) { assert.equal(node.hidden,true); assert.equal(node.disabled,true); }
  assert.equal(input.disabled,true); assert.equal(submit.disabled,true);
  for (const node of [image,download,search]) { assert.equal(node.hidden,false); assert.equal(node.disabled,false); }
  access.apply(container,reviewer); // Repeated render must not overwrite original states.
  access.apply(container,owner);
  assert.equal(edit.hidden,false); assert.equal(edit.disabled,false);
  assert.equal(chat.hidden,false); assert.equal(input.disabled,false);
  assert.equal(approve.disabled,true,'Image-dependent approval stays disabled until the image loads');
});

test('actual app renders reviewer badge/read navigation and rejects crafted mutation events before transport', async () => {
  const app = await application();
  assert.equal(app.nodes.get('#access-banner').hidden,false);
  assert.equal(app.nodes.get('#access-label').textContent,'Наблюдатель');
  assert.match(app.nodes.get('#main').innerHTML,/data-action="filter"/);
  assert.doesNotMatch(app.nodes.get('#main').innerHTML,/data-action="(?:manual-post|automation)"/);
  assert.throws(() => app.run('openForm("business","Создать","","","Создать")'), /Доступ для просмотра/);
  await assert.rejects(app.run('api("/business", {name:"Forbidden"})'),/Доступ для просмотра/);
  assert.throws(() => app.run('request("./api/business/one/pdf", {method:"POST"})'),/Доступ для просмотра/);
  await assert.rejects(app.run('uploadManualImage(null,"one",{})'),/Доступ для просмотра/);
  const control = {disabled:false,dataset:{action:'approve',id:'one',revision:'1'}};
  await app.listeners.get('document:click')({target:{closest:()=>control},preventDefault() {}});
  await app.listeners.get('#dialog:submit')({target:{},preventDefault() {}});
  await app.listeners.get('#main:submit')({target:{matches:selector => selector==='[data-chat]'},preventDefault() {}});
  assert.equal(app.requests.length,1,'Only the initial GET state reaches transport');
  assert.ok(app.requests.every(({options}) => options.method==='GET'));
  const post = {id:'p',business_id:'one',text:'Материал',status:'review',revision:1,
    profile_version:1,channel_id:'@example',scheduled_at:null,created_at:new Date().toISOString(),image_asset_id:'asset'};
  app.context.testPost = post;
  app.run('showPost(testPost)');
  const detail = app.nodes.get('#dialog-body').innerHTML;
  assert.match(detail,/Скачать изображение/);
  assert.match(detail,/Материал/);
  assert.doesNotMatch(detail,/data-action="(?:approve|reject|edit-post|replace-image|revise-image|revise-text)"/);
});

test('owner/operator and isolated preview retain editing; missing live grants fail closed', async () => {
  for (const access of [owner,{role:'operator',can_write:true}]) {
    const app = await application(access);
    assert.equal(app.nodes.get('#access-banner').hidden,true);
    assert.match(app.nodes.get('#main').innerHTML,/data-action="manual-post"/);
    await app.run('api("/business",{name:"Local fake"})');
    assert.equal(app.requests.at(-1).options.method,'POST');
  }
  const missing = await application(null);
  assert.equal(missing.run('canWrite()'),false);
  const preview = await application(undefined,true);
  assert.equal(preview.run('currentAccess().role'),'owner');
  assert.equal(preview.run('canWrite()'),true);
  assert.equal(preview.requests.length,0,'Preview never requests the connected API');
});

test('reviewer downloads the already loaded image with the correct PNG/JPEG extension', async () => {
  const app = await application();
  const clicked = [];
  app.context.document.createElement = () => ({click() { clicked.push([this.href,this.download]); },remove() {}});
  app.nodes.get('#dialog-body').querySelector = () => ({src:'blob:local-image',closest:() => ({dataset:{asset:'asset'}})});
  const control = {disabled:false,dataset:{action:'download-image'}};
  for (const mime of ['image/png','image/jpeg']) {
    app.context.testMime = mime;
    app.run('imageTypes.set("asset", testMime)');
    await app.listeners.get('document:click')({target:{closest:()=>control},preventDefault() {}});
  }
  assert.deepEqual(clicked,[['blob:local-image','post-image.png'],['blob:local-image','post-image.jpg']]);
  assert.equal(app.requests.length,1,'Downloads reuse loaded bytes without a new network request');
});
