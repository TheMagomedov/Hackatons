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
  await page.getByRole('button',{name:'Собрать профиль',exact:true}).click();
  await page.getByRole('button',{name:'Подтвердить',exact:true}).click();
  await page.getByRole('button',{name:'Подключить канал',exact:true}).click();
  await page.locator('input[name="value"]').fill('@qa_channel');
  await page.getByRole('button',{name:'Проверить канал',exact:true}).click();
  await page.locator('.dock [data-view="content"]').click();
  await page.getByRole('button',{name:'Создать',exact:true}).click();
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
  assert.deepEqual(errors,[]);
  console.log('PASS live HTML + signed Telegram session + real API/backend: no-code onboarding, preparation worker, image, schedule, approve, AI revise/reset approval, automation, persisted chat history. Demo providers only; no publishing worker.');
 } finally {if(browser) await browser.close(); await stopFixture(fixture.child);}
})().catch(e=>{console.error(e);process.exitCode=1;});
