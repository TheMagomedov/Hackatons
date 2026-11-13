const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../app/miniapp/static/transport.js'), 'utf8');

function client(fetch, online = true, timers = {setTimeout, clearTimeout}) {
  const context = {window: {fetch, navigator: {onLine: online}}, AbortController, Response, ...timers};
  vm.runInNewContext(source, context);
  return context.window.TirazhHTTP;
}

(async () => {
  let calls = 0;
  const response = new Response('{"error":"conflict"}', {status: 409, headers: {'Content-Type': 'application/json'}});
  const received = await client(async (url, options) => {
    calls++;
    assert.equal(url, '/api/post');
    assert.equal(options.method, 'POST');
    assert.equal(options.body, 'payload');
    assert.equal(options.headers.authorization, 'test');
    assert.equal(options.signal.aborted, false);
    return response;
  }).request('/api/post', {method: 'POST', body: 'payload', headers: {authorization: 'test'}});
  assert.equal(received.status, 409);
  assert.equal(received.headers.get('Content-Type'), 'application/json');
  assert.deepEqual(await received.json(), {error:'conflict'});
  assert.equal(calls, 1, 'Conflict responses must not retry');

  const deadlines=[];
  const timed=client(async () => new Response('{}'),true,{
    setTimeout(_callback,delay) {deadlines.push(delay);return 1;},clearTimeout() {},
  });
  await timed.request('/api/state');
  await timed.request('/api/business',{method:'POST'});
  await timed.request('/api/chat',{method:'POST',timeoutMs:330000});
  assert.deepEqual(deadlines,[20000,150000,330000],'Only explicit long-running calls extend the default budget');
  await assert.rejects(timed.request('/api/chat',{method:'POST',timeoutMs:330001}),/Invalid request timeout/);

  calls = 0;
  await assert.rejects(client((url, {signal}) => {
    calls++;
    return new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new Error('aborted'))));
  }).request('/api/prepare', {method: 'POST', timeoutMs: 10}), /действие могло сохраниться/);
  assert.equal(calls, 1, 'Timeout must not repeat a paid or publishing action');

  await assert.rejects(client(async (url, {signal}) => new Response(new ReadableStream({
    start(controller) {signal.addEventListener('abort', () => controller.error(new Error('body stalled')));},
  }))).request('/api/asset', {timeoutMs:10}), error => /Данные не загрузились/.test(error.message) && !/действие могло сохраниться/.test(error.message));

  await assert.rejects(client(async () => {throw new TypeError('Failed to fetch');}, false).request('/api'), /Нет подключения/);
  await assert.rejects(client(async () => {throw new TypeError('Failed to fetch');}).request('/api'), /Не удалось связаться/);
  const cancellation = new AbortController();
  const abortError = new Error('caller cancelled');
  const pending = client((url, {signal}) => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(abortError));
  })).request('/api', {signal: cancellation.signal});
  cancellation.abort();
  await assert.rejects(pending, error => error === abortError);
  console.log('PASS: request transport preserves responses, aborts timeouts, explains offline state, and never retries mutations.');
})().catch(error => {console.error(error); process.exitCode = 1;});
