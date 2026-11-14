// Isolated browser acceptance for VK session exchange; never connects to VK or a live database.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAO0lEQVR4nO3RQREAMAjEwKNiK6ty0FUJ4cMvK+CYCdXvZtNZXY8HBvwBMhEyETIRMhEyETIRMhEyUcgHP4UCCnaFLHEAAAAASUVORK5CYII=', 'base64');
async function stopFixture(child) {
  if(child.exitCode!==null) return;
  await new Promise(resolve=>{
    const timeout=setTimeout(()=>{child.kill('SIGKILL');resolve();},5000);
    child.once('exit',()=>{clearTimeout(timeout);resolve();});child.kill('SIGTERM');
  });
}
async function startFixture(role) {
  const repo=path.resolve(__dirname,'../..');
  const child=spawn(process.env.PYTHON_EXECUTABLE||'python',
    ['tests/ui/backend_fixture.py','--role',role,'--vk'],
    {cwd:repo,env:{...process.env,PYTHONPATH:repo,PYTHONUTF8:'1'},stdio:['ignore','pipe','pipe']});
  try {
    let output='',fixtureErrors='';
    child.stderr.on('data',part=>{fixtureErrors+=part.toString();});
    const launch=await new Promise((resolve,reject)=>{
      const timeout=setTimeout(()=>reject(Error('Fixture startup failed: '+fixtureErrors)),25000);
      child.once('error',error=>{clearTimeout(timeout);reject(error);});
      child.once('exit',code=>{clearTimeout(timeout);reject(Error('Fixture exit '+code+': '+fixtureErrors));});
      child.stdout.on('data',part=>{
        output+=part.toString();
        const match=output.match(/UI_VK_READY (http:\/\/127\.0\.0\.1:\d+\/mini\/#studioLaunch=[A-Za-z0-9_-]{43})/);
        if(match){clearTimeout(timeout);resolve(match[1]);}
      });
    });
    return {child,launch};
  } catch(error) {await stopFixture(child);throw error;}
}
async function downloadImage(page) {
  const pending=page.waitForEvent('download');
  await page.getByRole('button',{name:'Скачать изображение',exact:true}).click();
  const download=await pending;
  assert.equal(download.suggestedFilename(),'post-image.png');
  assert.equal(await download.failure(),null);
  const content=await fs.readFile(await download.path());
  await download.delete();
  return content;
}
async function checkRole(browser,role) {
  const {child,launch}=await startFixture(role);
  let context;
  try {
    const base=launch.split('#')[0],origin=new URL(base).origin;
    context=await browser.newContext({viewport:{width:390,height:844},reducedMotion:'reduce'});
    // Every page, including replay, is confined to its isolated loopback fixture.
    await context.route('**/*',route=>new URL(route.request().url()).origin===origin?route.continue():route.abort());
    const page=await context.newPage();
    let session=''; const errors=[],requests=[];
    context.on('page',opened=>opened.on('pageerror',error=>errors.push(error.message)));
    page.on('pageerror',error=>errors.push(error.message));
    page.on('request',request=>{
      if(request.url().includes('/mini/api/')) {
        session ||= request.headers()['x-studio-session'];
        assert.ok(session);
        assert.equal(request.headers()['x-studio-session'],session);
        assert.equal(request.headers()['x-telegram-init-data'],undefined);
      }
      requests.push({url:request.url(),method:request.method()});
    });
    const api=async suffix=>{
      const response=await page.request.get(base+'api/'+suffix,{headers:{'X-Studio-Session':session}});
      assert.equal(response.status(),200,suffix);
      return response;
    };
    await page.goto(launch);
    await page.locator('#main[data-screen="overview"]').waitFor();
    assert.equal(new URL(page.url()).hash,'#overview');
    assert.equal(await page.evaluate(()=>window.TirazhSession.initData()),'');
    const business=await page.locator('#workspace').inputValue();
    await page.locator('nav [data-view="content"]:visible').click();
    await page.locator('#main[data-screen="content"]').waitFor();
    if(role==='reviewer') {
      assert.match(await page.locator('#access-label').textContent(),/Наблюдатель/);
      const postId=await page.locator('.post-card').first().getAttribute('data-id');
      const post=await(await api('post/'+postId)).json();
      const expected=await(await api('asset/'+post.image_asset_id)).body();
      assert.deepEqual(expected.subarray(0,8),Buffer.from([137,80,78,71,13,10,26,10]));
      await page.locator('.post-card').first().click();
      await page.waitForFunction(()=>document.querySelector('.preview-art img')?.naturalWidth>0);
      assert.deepEqual(await downloadImage(page),expected,'VK reviewer downloads the exact stored PNG');
      assert.equal(await page.locator('#dialog-body [data-write]').count(),0);
      const write=await page.request.post(base+'api/business',{
        headers:{'X-Studio-Session':session},data:{name:'Should not be created'},
      });
      assert.equal(write.status(),403);
    } else {
      const before=await(await api('business/'+business+'/control')).json();
      await page.getByRole('button',{name:'Создать вручную',exact:true}).click();
      await page.locator('textarea[name="text"]').fill('Ручной PNG через изолированную сессию ВК');
      await page.locator('input[name="image"]').setInputFiles({name:'vk-manual.png',mimeType:'image/png',buffer:png});
      const saved=page.waitForResponse(response=>response.url().endsWith('/draft')&&response.request().method()==='POST');
      await page.getByRole('button',{name:'Сохранить черновик',exact:true}).click();
      const savedResponse=await saved;
      assert.equal(savedResponse.status(),200);
      const post=await savedResponse.json();
      assert.equal(post.business_id,business);
      assert.equal(post.content_origin,'manual');
      assert.equal(post.status,'review');
      assert.equal(post.revision,1);
      assert.equal(post.approved_revision,null);
      await page.waitForFunction(()=>document.querySelector('.preview-art img')?.naturalWidth===32);
      assert.deepEqual(await(await api('asset/'+post.image_asset_id)).body(),png,'VK upload persists exact PNG bytes');
      assert.deepEqual(await downloadImage(page),png,'VK download preserves uploaded PNG bytes');
      const writes=requests.filter(request=>request.method==='POST'&&request.url.includes('/mini/api/'));
      assert.deepEqual(writes.map(request=>new URL(request.url).pathname),[
        '/mini/api/business/'+business+'/image','/mini/api/business/'+business+'/draft',
      ],'Manual VK upload creates one asset and one draft without AI calls');
      const after=await(await api('business/'+business+'/control')).json();
      assert.deepEqual(after.operations,before.operations,'Manual workflow adds no AI operations');
    }
    await page.getByRole('button',{name:'Закрыть',exact:true}).click();
    const reportResponse=page.waitForResponse(response=>response.url().endsWith('/business/'+business+'/report'));
    await page.locator('nav [data-view="reports"]:visible').click();
    await page.locator('#main[data-screen="reports"]').waitFor();
    const response=await reportResponse;
    assert.equal(response.status(),200);
    const report=await response.json();
    assert.equal(await page.locator('.report-detail pre').textContent(),report.detail);
    assert.equal(report.series.length,7);
    assert.equal(report.series.reduce((total,day)=>total+day.sent,0),0);
    const posts=await(await api('business/'+business+'/posts')).json();
    assert.ok(posts.items.length>0);
    assert.ok(posts.items.every(post=>post.published_at===null),'Fixture never publishes');
    assert.equal(requests.filter(request=>request.url.endsWith('/mini/session/vk')).length,1);
    const replay=await context.newPage();
    await replay.goto(launch);
    await replay.locator('#main[data-screen="error"]').waitFor();
    assert.match(await replay.locator('#main').innerText(),/Отправьте \/menu боту/);
    assert.deepEqual(errors,[]);
    console.log('VK studio '+role+': exact PNG, report, access and one-use replay — PASS');
  } finally {
    if(context) await context.close();
    await stopFixture(child);
  }
}
async function main() {
  const browser=await chromium.launch();
  try {for(const role of ['reviewer','owner']) await checkRole(browser,role);}
  finally {await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
