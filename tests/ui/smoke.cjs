// Run the preview server first: python -m app miniapp-preview --port 8765.
// Install Playwright separately, then run: node tests/ui/smoke.cjs.
// Optional: PLAYWRIGHT_MODULE=/absolute/path/to/playwright MINI_APP_URL=http://127.0.0.1:8765/mini/
// This exercises the explicitly labelled UI preview. No live AI or publications occur.
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const url = process.env.MINI_APP_URL || 'http://127.0.0.1:8765/mini/';
(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport:{width:390,height:844}, reducedMotion:'reduce'});
  const errors=[];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(url);
    assert.equal(await page.locator('body').getAttribute('data-mode'), 'preview', 'Only run this workflow on the isolated preview');
    await page.getByRole('heading',{name:'Идеи — в дело.'}).waitFor();
    assert.match(await page.locator('#mode-banner').innerText(), /имитируются/);
    for(const width of [320,390,768,1440]) {
      await page.setViewportSize({width,height:900});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth),true,`No overflow at ${width}`);
    }
    await page.setViewportSize({width:390,height:844});
    const nav = name => page.locator(`.dock [data-view="${name}"]`).click();
    await nav('work');
    await page.getByRole('textbox',{name:'Сообщение AI-маркетологу'}).fill('Какие факты мы знаем о кофейне?');
    await page.getByRole('button',{name:'Отправить сообщение',exact:true}).click();
    await page.locator('.chat-message.assistant').waitFor();
    assert.match(await page.locator('.chat-message.assistant').innerText(), /Тёплое зерно/);
    await page.getByRole('button',{name:'Настроить',exact:true}).click();
    await page.locator('input[name="enabled"]').check();
    await page.locator('input[name="interval_hours"]').fill('12');
    await page.getByRole('button',{name:'Сохранить расписание'}).click();
    await page.getByText('Каждые 12 ч', {exact:false}).waitFor();
    await page.getByRole('button',{name:'Поставить на паузу',exact:true}).click();
    await page.getByRole('heading',{name:'Публикации на паузе'}).waitFor();
    await page.getByRole('button',{name:'Возобновить',exact:true}).click();
    await page.getByRole('heading',{name:'Публикации разрешены'}).waitFor();
    await nav('content');
    await page.locator('[data-action="open-post"][data-id="morning"]').click();
    assert.match(await page.locator('.publication-target').innerText(),/@teploe_zerno/);
    await page.getByRole('button',{name:'Текст и время',exact:true}).click();
    await page.locator('input[name="scheduled_at"]').fill('2099-12-01T12:00');
    await page.getByRole('button',{name:'Сохранить версию',exact:true}).click();
    await page.getByRole('button',{name:'Согласовать версию 2',exact:true}).click();
    await page.locator('#dialog-body .status.approved').waitFor();
    await page.getByRole('button',{name:'Правка текста AI',exact:true}).click();
    await page.locator('textarea[name="feedback"]').fill('Сделай короче');
    await page.getByRole('button',{name:'Подготовить новую версию',exact:true}).click();
    await page.locator('#dialog-body .status.review').waitFor();
    await page.getByRole('button',{name:'Согласовать версию 3',exact:true}).waitFor();
    assert.match(await page.locator('.post-text').innerText(),/Демо-правка/);
    await page.getByRole('button',{name:'Правка изображения AI',exact:true}).click();
    await page.locator('textarea[name="feedback"]').fill('Добавь тёплый свет');
    await page.getByRole('button',{name:'Подготовить новую версию',exact:true}).click();
    await page.getByRole('button',{name:'Согласовать версию 4',exact:true}).waitFor();
    await page.getByRole('button',{name:'Отклонить',exact:true}).click();
    await page.locator('#dialog-body .status.rejected').waitFor();
    await page.getByRole('button',{name:'Закрыть',exact:true}).click();
    await nav('business');
    await page.getByRole('button',{name:'Новый бизнес',exact:true}).click();
    await page.locator('input[name="name"]').fill('Стекольная мастерская');
    await page.getByRole('button',{name:'Создать бизнес',exact:true}).click();
    await page.getByRole('heading',{name:'Стекольная мастерская'}).waitFor();
    await page.setViewportSize({width:320,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth),true,'Long company name fits narrow header');
    await page.setViewportSize({width:390,height:844});
    await nav('work');
    assert.equal(await page.locator('.chat-message').count(),0,'New business cannot see another company conversation');
    await nav('content');
    assert.equal(await page.locator('.post-card').count(),0,'New business cannot see another company posts');
    await nav('overview');
    await page.getByRole('heading',{name:'Ещё 3 шага до первого поста'}).waitFor();
    await page.getByRole('button',{name:'Материалы',exact:false}).click();
    await page.locator('textarea[name="value"]').fill('Производим стеклянные перегородки для офисов и домов.');
    await page.getByRole('button',{name:'Сохранить источник',exact:true}).click();
    await nav('business');
    await page.getByRole('button',{name:'Собрать с AI',exact:true}).click();
    await page.getByRole('button',{name:'Подтвердить',exact:true}).click();
    await page.getByRole('button',{name:'Подключить канал',exact:true}).click();
    await page.locator('input[name="value"]').fill('@glass_workshop_test');
    await page.getByRole('button',{name:'Проверить канал',exact:true}).click();
    await nav('content');
    await page.getByRole('button',{name:'С AI',exact:true}).click();
    await page.locator('textarea[name="request"]').fill('Пост о стеклянной перегородке для офиса');
    await page.getByRole('button',{name:'Поставить в очередь',exact:true}).click();
    await page.locator('.post-card').waitFor();
    assert.equal(await page.locator('.post-card').count(),1);
    await page.locator('#workspace').selectOption({label:'Тёплое зерно'});
    await nav('work');
    assert.match(await page.locator('.chat-message.user').innerText(), /Какие факты/);
    await page.getByRole('button',{name:'Сменить тему',exact:true}).click();
    assert.equal(await page.locator('body').getAttribute('data-theme'),'light');
    assert.deepEqual(errors, [], 'No JavaScript errors');
    console.log('PASS: responsive 320–1440, chat isolation, automation, pause, revision approval, AI edits, rejection, no-code company onboarding, theme.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
