// npm run test:ui:backend — starts an isolated local Python demo backend and stops it on exit.
// Set PYTHON_EXECUTABLE to your virtualenv Python when it is not on PATH.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const { spawn } = require('node:child_process');
const path = require('node:path');
const data={auth_date:String(Math.floor(Date.now()/1000)),user:JSON.stringify({id:42,first_name:'QA'}),query_id:'local-browser-qa'};
const secret=crypto.createHmac('sha256','WebAppData').update('12345:test-only-key').digest();
const check=Object.keys(data).sort().map(k=>`${k}=${data[k]}`).join('\n');
data.hash=crypto.createHmac('sha256',secret).update(check).digest('hex');
const initData=new URLSearchParams(data).toString();
async function startFixture() {
 const repo = path.resolve(__dirname, '../..');
 const child = spawn(process.env.PYTHON_EXECUTABLE || 'python', [path.join(__dirname, 'backend_fixture.py')], {
  cwd: repo, env: {...process.env, PYTHONPATH: repo}, stdio:['ignore','pipe','pipe'],
 });
 let output='';
 child.stderr.on('data', part=>{output+=part.toString();});
 try {
  const url = await new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>reject(new Error('Test backend did not start: '+output)),20000);
   child.once('error',error=>{clearTimeout(timer);reject(error);});
   child.once('exit',code=>{clearTimeout(timer);reject(new Error('Test backend exited '+code+': '+output));});
   let stdout='';
   child.stdout.on('data',part=>{
    stdout+=part.toString();
    const match=stdout.match(/UI_BACKEND_READY (http:\/\/127\.0\.0\.1:\d+\/mini\/)/);
    if(match){clearTimeout(timer);resolve(match[1]);}
   });
  });
  return {child,url};
 } catch(error) {child.kill();throw error;}
}
async function stopFixture(child) {
 if(child.exitCode !== null) return;
 await new Promise(resolve=>{
  const timer=setTimeout(()=>{child.kill('SIGKILL');resolve();},5000);
  child.once('exit',()=>{clearTimeout(timer);resolve();});
  child.kill('SIGTERM');
 });
}
(async()=>{
 const fixture=await startFixture();
 let browser;
 try {
 browser=await chromium.launch();
 const page=await browser.newPage({viewport:{width:390,height:844},reducedMotion:'reduce'});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.addInitScript(()=>{localStorage.setItem('tirazh.preview.v1','untouched-legacy-sentinel');localStorage.setItem('salavatsteklo.preview.v1','untouched-live-sentinel');});
 let releaseImages;
 const imageGate = new Promise(resolve=>{releaseImages=resolve;});
 await page.route('**/mini/api/asset/**',async route=>{await imageGate;await route.continue();});
 await page.route('https://telegram.org/js/telegram-web-app.js',route=>route.fulfill({contentType:'application/javascript',body:`window.Telegram={WebApp:{initData:${JSON.stringify(initData)},colorScheme:'dark',isVersionAtLeast:()=>false,ready:()=>{},expand:()=>{},onEvent:()=>{}}};`}));
  await page.goto(fixture.url);
  await page.getByRole('button',{name:'Создать бизнес',exact:true}).click();
  await page.locator('input[name="name"]').fill('Реальный API · тестовая компания');
  await page.locator('#dialog').getByRole('button',{name:'Создать бизнес',exact:true}).click();
  await page.getByRole('button',{name:'Добавить источник',exact:true}).click();
  await page.locator('textarea[name="value"]').fill('Мы производим стеклянные перегородки для офисов в Уфе.');
  await page.getByRole('button',{name:'Сохранить источник',exact:true}).click();
  await page.getByRole('button',{name:'Собрать с AI',exact:true}).click();
  await page.getByRole('button',{name:'Подтвердить',exact:true}).click();
  await page.getByRole('button',{name:'Подключить канал',exact:true}).click();
  await page.locator('input[name="value"]').fill('@qa_channel');
  await page.getByRole('button',{name:'Проверить канал',exact:true}).click();
  await page.locator('.dock [data-view="content"]').click();
  await page.getByRole('button',{name:'С AI',exact:true}).click();
  await page.locator('textarea[name="request"]').fill('Один пост о компании');
  await page.getByRole('button',{name:'Поставить в очередь',exact:true}).click();
  await page.locator('.post-card').first().waitFor({timeout:15000});
  await page.locator('.post-card').first().click();
  await page.getByRole('button',{name:'Текст и время',exact:true}).click();
  await page.locator('input[name="scheduled_at"]').fill('2099-12-01T12:00');
  await page.getByRole('button',{name:'Сохранить версию',exact:true}).click();
  const approve=page.getByRole('button',{name:/Согласовать версию/});
  await approve.waitFor();
  assert.equal(await approve.isEnabled(),false,'Cannot approve before the publication image is visible');
  releaseImages();
  await page.waitForFunction(()=>document.querySelector('.preview-art img')?.naturalWidth>0);
  assert.equal(await approve.isEnabled(),true,'Image must load before approval');
  await approve.click();
  await page.locator('#dialog-body .status.approved').waitFor();
  await page.getByRole('button',{name:'Правка текста AI',exact:true}).click();
  await page.locator('textarea[name="feedback"]').fill('Сделай короче');
  await page.getByRole('button',{name:'Подготовить новую версию',exact:true}).click();
  await page.locator('#dialog-body .status.review').waitFor();
  await page.getByRole('button',{name:'Закрыть',exact:true}).click();
  await page.locator('.dock [data-view="work"]').click();
  await page.getByRole('button',{name:'Настроить',exact:true}).click();
  await page.locator('input[name="enabled"]').check();
  await page.locator('input[name="interval_hours"]').fill('24');
  await page.getByRole('button',{name:'Сохранить расписание',exact:true}).click();
  await page.getByText('Каждые 24 ч',{exact:false}).waitFor();
  await page.getByRole('textbox',{name:'Сообщение AI-маркетологу'}).fill('Подготовь пост о перегородках');
  await page.getByRole('button',{name:'Отправить сообщение',exact:true}).click();
  await page.locator('.chat-message.user').waitFor();
  assert.match(await page.locator('.chat-message.user').innerText(),/перегородках/);
  await page.reload();
  await page.locator('.chat-message.user').waitFor();
  assert.match(await page.locator('.chat-message.user').innerText(),/перегородках/,'Persisted history survives browser reload');
  // Exercise the separate manual HTTP/multipart path against SQLite, without model calls.
  const manualCalls=[];
  page.on('request',request=>{if(request.method()==='POST') manualCalls.push(new URL(request.url()).pathname);});
  await page.locator('.dock [data-view="business"]').click();
  await page.getByRole('button',{name:'Новый бизнес',exact:true}).click();
  await page.locator('input[name="name"]').fill('Ручной API-путь без AI');
  await page.locator('#dialog').getByRole('button',{name:'Создать бизнес',exact:true}).click();
  await page.getByRole('button',{name:'Заполнить вручную',exact:true}).click();
  await page.locator('textarea[name="description"]').fill('Производим офисные перегородки из стекла.');
  await page.locator('textarea[name="products"]').fill('Перегородки\nСтеклянные двери');
  await page.locator('textarea[name="constraints"]').fill('Не обещать скидки');
  await page.getByRole('button',{name:'Сохранить профиль',exact:true}).click();
  await page.getByRole('button',{name:'Подтвердить',exact:true}).click();
  await page.getByRole('button',{name:'Подключить канал',exact:true}).click();
  await page.locator('input[name="value"]').fill('@manual_test_channel');
  await page.getByRole('button',{name:'Проверить канал',exact:true}).click();
  const manualBusiness=await page.locator('#workspace').inputValue();
  await page.locator('.dock [data-view="content"]').click();
  await page.getByRole('button',{name:'Создать',exact:true}).click();
  await page.locator('textarea[name="text"]').fill('Ручной пост с настоящим сохранением изображения');
  const manualImage={name:'manual.png',mimeType:'image/png',buffer:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAO0lEQVR4nO3RQREAMAjEwKNiK6ty0FUJ4cMvK+CYCdXvZtNZXY8HBvwBMhEyETIRMhEyETIRMhEyUcgHP4UCCnaFLHEAAAAASUVORK5CYII=','base64')};
  await page.locator('input[name="image"]').setInputFiles(manualImage);
  let releaseUpload;
  const uploadGate=new Promise(resolve=>{releaseUpload=resolve;});
  await page.route('**/mini/api/business/*/image',async route=>{await uploadGate;await route.continue();});
  const savedDraft=page.waitForResponse(response=>response.url().endsWith('/draft') && response.request().method()==='POST');
  await page.getByRole('button',{name:'Сохранить черновик',exact:true}).click();
  assert.equal(await page.locator('input[name="image"]').isDisabled(),true,'Cannot change selected image during upload');
  await page.getByRole('button',{name:'Закрыть',exact:true}).click();
  await page.getByRole('button',{name:'Создать',exact:true}).click();
  await page.locator('textarea[name="text"]').fill('Новый открытый редактор');
  releaseUpload();
  const manualPost=await(await savedDraft).json();
  await page.locator('#toast').getByText('Черновик сохранён. Он не согласован к публикации.',{exact:true}).waitFor();
  assert.equal(await page.locator('textarea[name="text"]').inputValue(),'Новый открытый редактор','Old upload completion preserves the new dialog');
  await page.getByRole('button',{name:'Закрыть',exact:true}).click();
  await page.locator('[data-action="open-post"][data-id="'+manualPost.id+'"]').click();
  assert.equal(manualPost.content_origin,'manual');assert.equal(manualPost.scheduled_at,null);assert.equal(manualPost.status,'review');
  await page.waitForFunction(()=>document.querySelector('.preview-art img')?.naturalWidth===32);
  await page.getByRole('button',{name:'Текст и время',exact:true}).click();
  await page.locator('textarea[name="text"]').fill('Ручной текст исправлен без назначения даты');
  await page.getByRole('button',{name:'Сохранить версию',exact:true}).click();
  await page.locator('#dialog-body .status.review').waitFor();
  assert.match(await page.locator('.publication-target').innerText(),/Без даты/);
  await page.getByRole('button',{name:'Текст и время',exact:true}).click();
  await page.locator('input[name="scheduled_at"]').fill('2099-12-01T12:00');
  await page.getByRole('button',{name:'Сохранить версию',exact:true}).click();
  await page.getByRole('button',{name:/Согласовать версию/}).click();
  await page.locator('#dialog-body .status.approved').waitFor();
  await page.getByRole('button',{name:'Заменить изображение',exact:true}).click();
  await page.locator('input[name="image"]').setInputFiles(manualImage);
  await page.getByRole('button',{name:'Сохранить новую версию',exact:true}).click();
  await page.locator('#dialog-body .status.review').waitFor();
  assert.equal(manualCalls.some(url=>/\/(prepare|analyze|chat|revise-text|revise-image)$/.test(url)),false,'Manual workflow sends no AI requests');
  const control=await(await page.request.get(fixture.url+'api/business/'+manualBusiness+'/control',{headers:{'X-Telegram-Init-Data':initData}})).json();
  assert.equal(control.operations.length,0,'No AI operations recorded for manual company');
  const cal=await(await page.request.get(fixture.url+'api/business/'+manualBusiness+'/calendar?month=2099-12',{headers:{'X-Telegram-Init-Data':initData}})).json();
  assert.equal(cal.items.length,1);assert.equal(cal.items[0].status,'review');
  assert.equal(await page.evaluate(()=>localStorage.getItem('salavatsteklo.preview.v1')),'untouched-live-sentinel','Live UI never overwrites demo storage');
  assert.equal(await page.evaluate(()=>localStorage.getItem('tirazh.preview.v1')),'untouched-legacy-sentinel','Live UI preserves unrelated previous demo');
  let releaseEditRead;
  const editGate=new Promise(resolve=>{releaseEditRead=resolve;});
  const postUrl='**/mini/api/post/'+manualPost.id;
  await page.route(postUrl,async route=>{await editGate;await route.continue();});
  const delayedRead=page.waitForResponse(response=>response.url().endsWith('/post/'+manualPost.id)&&response.request().method()==='GET');
  await page.getByRole('button',{name:'Текст и время',exact:true}).click();
  await page.getByRole('button',{name:'Закрыть',exact:true}).click();
  const anotherBusiness=await page.locator('#workspace option').evaluateAll((options,id)=>options.find(option=>option.value!==id).value,manualBusiness);
  await page.locator('#workspace').selectOption(anotherBusiness);
  releaseEditRead();await delayedRead;
  await page.waitForTimeout(100);
  assert.equal(await page.locator('#dialog').evaluate(dialog=>dialog.open),false,'Late editor read cannot open after company switch');
  assert.deepEqual(errors,[]);
  console.log('PASS live HTML + signed Telegram session + real API/backend: no-code onboarding, preparation worker, image, schedule, approve, AI revise/reset approval, automation, persisted chat history, manual profile/draft/multipart image replacement with zero AI operations. Demo providers only; no publishing worker.');
 } finally {if(browser) await browser.close(); await stopFixture(fixture.child);}
})().catch(e=>{console.error(e);process.exitCode=1;});
