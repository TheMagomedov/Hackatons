// Isolated browser acceptance for VK session exchange; never connects to VK or a live database.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const path = require('node:path');
const {chromium} = require('playwright');
async function main() {
  const repo=path.resolve(__dirname,'../..');
  const child=spawn(process.env.PYTHON_EXECUTABLE||'python',
    ['tests/ui/backend_fixture.py','--role','reviewer','--vk'],
    {cwd:repo,env:{...process.env,PYTHONPATH:repo,PYTHONUTF8:'1'},stdio:['ignore','pipe','pipe']});
  let browser;
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
    browser=await chromium.launch();
    const page=await browser.newPage({viewport:{width:390,height:844}});
    await page.route('https://**/*',route=>route.abort());
    let session=''; const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    page.on('request',request=>{
      if(request.url().includes('/mini/api/')) {
        session=request.headers()['x-studio-session'];
        assert.ok(session);
        assert.equal(request.headers()['x-telegram-init-data'],undefined);
      }
    });
    await page.goto(launch);
    await page.locator('#main[data-screen="overview"]').waitFor();
    assert.equal(new URL(page.url()).hash,'#overview');
    assert.match(await page.locator('#access-label').textContent(),/Наблюдатель/);
    await page.locator('nav [data-view="content"]:visible').click();
    await page.locator('#main[data-screen="content"]').waitFor();
    const base=launch.split('#')[0];
    const write=await page.request.post(base+'api/business',{
      headers:{'X-Studio-Session':session},data:{name:'Should not be created'},
    });
    assert.equal(write.status(),403);
    assert.equal(await page.evaluate(()=>window.TirazhSession.initData()),'');
    const replay=await browser.newPage();
    await replay.route('https://**/*',route=>route.abort());
    await replay.goto(launch);
    await replay.locator('#main[data-screen="error"]').waitFor();
    assert.match(await replay.locator('#main').innerText(),/Отправьте \/menu боту/);
    assert.deepEqual(errors,[]);
    console.log('VK studio browser: login, navigation, reviewer restriction, one-use replay — PASS');
  } finally {
    if(browser) await browser.close();
    if(child.exitCode===null) {
      await new Promise(resolve=>{
        const timeout=setTimeout(()=>{child.kill('SIGKILL');resolve();},5000);
        child.once('exit',()=>{clearTimeout(timeout);resolve();});child.kill('SIGTERM');
      });
    }
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
