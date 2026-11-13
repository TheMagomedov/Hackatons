// Local preview only: no provider calls. Run the preview server before this check.
// PLAYWRIGHT_MODULE=/path/to/playwright UI_ARTIFACT_DIR=/path/to/screenshots node tests/ui/a11y.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const url = process.env.MINI_APP_URL || 'http://127.0.0.1:8765/mini/';
const origin = new URL(url).origin;
const output = process.env.UI_ARTIFACT_DIR;
const views = ['overview', 'content', 'work', 'reports', 'business'];

async function snapshot(page, name) {
  if (output) await page.screenshot({path:path.join(output, name + '.png'), fullPage:true});
}
async function noOverflow(page, context) {
  const dimensions = await page.evaluate(() => ({width:innerWidth, content:document.documentElement.scrollWidth}));
  assert.ok(dimensions.content <= dimensions.width + 1, `${context}: horizontal overflow ${JSON.stringify(dimensions)}`);
}
async function navigate(page, view) {
  await page.locator(`nav:visible [data-view="${view}"]`).first().click();
  await page.locator(`#main[data-screen="${view}"]`).waitFor();
}
async function targetSizes(page, context) {
  const small = await page.evaluate(() => [...document.querySelectorAll(
    '.brand,#workspace,.icon-button,.desktop-nav button,.dock button,.button,.text-button,.filter,summary'
  )].flatMap(element => {
    const rect = element.getBoundingClientRect();
    return rect.width && rect.height && (rect.width < 43.9 || rect.height < 43.9)
      ? [{label:element.innerText || element.getAttribute('aria-label'), width:rect.width, height:rect.height}] : [];
  }));
  assert.deepEqual(small, [], `${context}: touch targets should be at least 44×44 CSS px`);
}
async function paletteContrast(page) {
  const ratios = await page.evaluate(() => {
    const style = getComputedStyle(document.body);
    function luminance(hex) {
      const channels = hex.trim().replace('#', '').match(/.{2}/g).map(v => parseInt(v, 16) / 255)
        .map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4);
      return channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722;
    }
    function contrast(foreground, background) {
      const a = luminance(style.getPropertyValue(foreground));
      const b = luminance(style.getPropertyValue(background));
      return (Math.max(a,b) + .05) / (Math.min(a,b) + .05);
    }
    return ['--ink','--muted','--subtle','--amber','--blue','--mint','--danger'].flatMap(color =>
      ['--app','--surface','--raised'].map(background => ({color,background,ratio:contrast(color,background)}))
    ).concat({color:'--on-accent',background:'--accent',ratio:contrast('--on-accent','--accent')});
  });
  assert.deepEqual(ratios.filter(item => item.ratio < 4.5), [], 'Palette text meets 4.5:1 on its base surfaces');
}
async function brandPresentation(page, theme, width) {
  await page.evaluate(() => document.fonts.ready);
  const observed = await page.evaluate(() => {
    const logos = [...document.querySelectorAll('.brand-logo')];
    const visible = logos.filter(image => getComputedStyle(image).display !== 'none');
    const loaded = [...document.fonts].filter(font => font.status === 'loaded').map(font => font.family.replaceAll('"',''));
    const result = {
      logos:logos.map(image => ({name:image.getAttribute('src'),loaded:image.complete && image.naturalWidth === 1182})),
      visible:visible.map(image => image.getAttribute('src')), loaded,
      bodyFont:getComputedStyle(document.body).fontFamily,
      headingFont:getComputedStyle(document.querySelector('.workspace-home h1')).fontFamily,
      obsoleteArtwork:Boolean(document.querySelector('#art-coffee,#art-pastry,#art-cup')),
      patterns:[...document.querySelectorAll('img[src*="brand-pattern-"]')].map(image => ({loaded:image.complete && image.naturalWidth > 0})),
    };
    if (visible.length === 1) {
      const image = visible[0].getBoundingClientRect();
      const window = document.querySelector('.brand-logo-window').getBoundingClientRect();
      // Original transparent PNG alpha bounds are 178,351–1107,848 in its 1182px canvas.
      const art = {left:image.left+image.width*178/1182,right:image.left+image.width*1107/1182,
        top:image.top+image.height*351/1182,bottom:image.top+image.height*848/1182};
      result.artHeight = art.bottom-art.top;
      result.clearSpace = [art.left-window.left,window.right-art.right,art.top-window.top,window.bottom-art.bottom];
    }
    return result;
  });
  assert.equal(observed.logos.length,2,'Both supplied logo variants are present');
  assert.ok(observed.logos.every(image => image.loaded),'Original logos load without fallback artwork');
  assert.deepEqual(observed.visible,[theme === 'light' ? './brand-logo.png' : './brand-logo-white.png'],`${theme}/${width}: correct logo contrast`);
  assert.ok(observed.artHeight >= 35,'Actual logo artwork meets the 35px minimum, excluding transparent canvas');
  assert.ok(observed.clearSpace.every(value => value >= observed.artHeight/7-1),`${theme}/${width}: preserve the O-height clear space ${JSON.stringify(observed.clearSpace)}`);
  assert.ok(observed.loaded.includes('Avenir Next Cyr') && observed.loaded.includes('Intro Black'),'Both supplied brand font families actually loaded');
  assert.match(observed.bodyFont,/Avenir Next Cyr/); assert.match(observed.headingFont,/Intro Black/);
  assert.equal(observed.obsoleteArtwork,false,'Previous coffee illustration symbols are removed');
  assert.ok(observed.patterns.length > 0 && observed.patterns.every(image => image.loaded),'Original branded patterns render in the dashboard');
}
async function filterKeyboard(page, context) {
  await navigate(page,'content');
  const filters = page.locator('.filter');
  await filters.first().focus();
  await page.keyboard.press('Shift+Tab');
  await page.keyboard.press('Tab');
  for (let step = 0; step < await filters.count(); step++) {
    if (step) await page.keyboard.press('Tab');
    const bounds = await page.evaluate(() => {
      const element = document.activeElement, container = element.closest('.filters');
      if (!container) return null;
      const rect = element.getBoundingClientRect(), parent = container.getBoundingClientRect();
      return {left:rect.left-parent.left,right:parent.right-rect.right,visible:element.matches(':focus-visible')};
    });
    assert.ok(bounds && bounds.visible && bounds.left >= 5 && bounds.right >= 5,
      `${context}: keyboard scrolls filters into view without clipping their focus outline ${JSON.stringify(bounds)}`);
  }
}
async function keyboardReachability(page, context) {
  await navigate(page, 'work');
  await page.locator('#main').focus();
  let reached = 0;
  for (let step = 0; step < 12; step++) {
    await page.keyboard.press('Tab');
    const focus = await page.evaluate(() => {
      const element = document.activeElement;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      const workspaceStyle = element.closest('.workspace') && getComputedStyle(element.closest('.workspace'));
      const dock = document.querySelector('.dock').getBoundingClientRect();
      return {tag:element.tagName,label:(element.innerText || element.getAttribute('aria-label') || '').slice(0,100), top:rect.top,bottom:rect.bottom,
        visible:element.matches(':focus-visible'), outline:parseFloat(style.outlineWidth),
        workspaceOutline:workspaceStyle ? parseFloat(workspaceStyle.outlineWidth) : 0,
        inNavigation:Boolean(element.closest('.topbar,.dock')),
        topbarBottom:document.querySelector('.topbar').getBoundingClientRect().bottom,
        bottomLimit:dock.height ? dock.top : innerHeight};
    });
    // Tabbing past the last document control can legitimately return focus to browser chrome.
    if (focus.tag === 'BODY') break;
    reached++;
    assert.ok(focus.visible && (focus.outline >= 2 || focus.workspaceOutline >= 2), `${context}: visible keyboard focus ${JSON.stringify(focus)}`);
    if (!focus.inNavigation) {
      assert.ok(focus.top >= focus.topbarBottom - 1 && focus.bottom <= focus.bottomLimit + 1,
        `${context}: focused control is obscured by sticky navigation ${JSON.stringify(focus)}`);
    }
  }
  assert.ok(reached >= 7, `${context}: keyboard reaches the complete task controls`);
}

(async () => {
  if (output) await fs.mkdir(output, {recursive:true});
  const browser = await chromium.launch({headless:true});
  const errors = [];
  let inspected = 0;
  try {
    for (const theme of ['dark','light']) {
      for (const width of [320,390,768,1440]) {
        const page = await browser.newPage({viewport:{width,height:840},reducedMotion:'reduce'});
        page.on('pageerror', error => errors.push(error.message));
        await page.route('**/*', route => new URL(route.request().url()).origin === origin
          ? route.continue() : route.abort());
        await page.goto(url);
        assert.equal(await page.locator('body').getAttribute('data-mode'),'preview','Run only against the isolated preview');
        await page.locator('.workspace-home').waitFor();
        if (await page.locator('body').getAttribute('data-theme') !== theme) {
          await page.getByRole('button',{name:'Сменить тему',exact:true}).click();
        }
        await page.waitForFunction(() => [...document.querySelectorAll('.brand-logo')].every(image => image.complete && image.naturalWidth > 0));
        await brandPresentation(page,theme,width);
        await paletteContrast(page);
        for (const view of views) {
          await navigate(page,view);
          await noOverflow(page,`${theme}/${width}/${view}`);
          await targetSizes(page,`${theme}/${width}/${view}`);
          await snapshot(page,`${theme}-${width}-${view}`);
          inspected++;
        }
        await keyboardReachability(page,`${theme}/${width}`);
        await filterKeyboard(page,`${theme}/${width}`);
        await page.close();
      }
    }
    const page = await browser.newPage({viewport:{width:320,height:480},reducedMotion:'reduce'});
    await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    await page.goto(url);
    await page.locator('.workspace-home').waitFor();
    await page.evaluate(() => {
      document.documentElement.style.setProperty('--tg-safe-area-inset-top','24px');
      document.documentElement.style.setProperty('--tg-safe-area-inset-bottom','34px');
    });
    await navigate(page,'content');
    await page.locator('[data-action="open-post"][data-id="morning"]').click();
    await page.locator('.post-text').evaluate(element => {element.textContent = 'Длинный материал для проверки прокрутки. '.repeat(40);});
    await page.locator('dialog').evaluate(element => {element.scrollTop = element.scrollHeight;});
    const modal = await page.evaluate(() => {
      const dialog = document.querySelector('dialog'), close = document.querySelector('.close');
      const bounds = dialog.getBoundingClientRect(), button = close.getBoundingClientRect();
      return {scrollable:dialog.scrollHeight > dialog.clientHeight, overflow:dialog.scrollWidth > dialog.clientWidth,
        closeVisible:button.top >= bounds.top && button.bottom <= bounds.bottom,
        safeTop:bounds.top >= 24,bodyLocked:getComputedStyle(document.documentElement).overflow === 'hidden'};
    });
    assert.deepEqual(modal,{scrollable:true,overflow:false,closeVisible:true,safeTop:true,bodyLocked:true});
    await snapshot(page,'dialog-320-short-viewport-safe-area');
    // Emulate Telegram reporting a smaller visible viewport while its layout viewport remains tall.
    await page.evaluate(() => document.documentElement.style.setProperty('--tg-viewport-height','300px'));
    await page.waitForFunction(() => document.querySelector('dialog').getBoundingClientRect().bottom <= 301,
      null,{timeout:2000});
    await snapshot(page,'dialog-320-telegram-viewport-height');
    await page.evaluate(() => document.documentElement.style.removeProperty('--tg-viewport-height'));
    for (let step = 0; step < 12; step++) {
      await page.keyboard.press('Tab');
      assert.equal(await page.evaluate(() => document.querySelector('dialog').matches(':modal')
        && (document.activeElement === document.body || Boolean(document.activeElement.closest('dialog')))),
      true,'Modal never gives keyboard focus to background controls');
    }
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('dialog').getAttribute('open'),null,'Escape closes modal');
    await navigate(page,'overview');
    await page.locator('.hero-description').evaluate(element => {element.textContent = 'Длинноеназваниекомпаниибезпробелов'.repeat(5);});
    await noOverflow(page,'long company name');
    await snapshot(page,'long-label-320-safe-area');
    await navigate(page,'business');
    await page.evaluate(() => window.scrollTo(0,document.documentElement.scrollHeight));
    const bottom = await page.evaluate(() => {
      const dock = document.querySelector('.dock').getBoundingClientRect();
      const buttons = [...document.querySelectorAll('#main button')];
      return {dockInset:innerHeight-dock.bottom,contentBottom:buttons.at(-1).getBoundingClientRect().bottom,dockTop:dock.top};
    });
    assert.ok(bottom.dockInset >= 34 && bottom.contentBottom <= bottom.dockTop,'Last control stays above the dock and safe area');
    await page.close();
    assert.deepEqual(errors,[],'No browser errors');
    console.log(`PASS: ${inspected} screen/theme/viewport checks, original logos and clear space, loaded brand fonts/patterns, 44px targets, text palette contrast, keyboard focus, modal scrolling, safe areas and long labels.`);
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exitCode=1;});
