"use strict";

const $ = (selector) => document.querySelector(selector);
const root = $("#main");
const dialog = $("#dialog");
const dialogBody = $("#dialog-body");
const workspace = $("#workspace");
const toast = $("#toast");
const demo = document.body.dataset.mode === "preview";
const accessRules = window.TirazhAccess;
const telegram = window.Telegram?.WebApp;
const insideTelegram = Boolean(!demo && telegram?.initData);
const labels = {
  review: "На согласовании", approved: "Запланирован", publishing: "Отправляется",
  published: "Опубликован", failed: "Ошибка", editing: "Редактируется",
  rejected: "Отклонён", delivery_unknown: "Проверьте доставку",
};
const editable = new Set(["review", "approved", "failed", "rejected"]);
const views = new Set(["overview", "content", "work", "reports", "business"]);
const icon = (name) => '<svg class="icon" aria-hidden="true"><use href="#i-' + name + '"></use></svg>';
const safe = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);
const path = encodeURIComponent;
const sampleDate = (days, hours) => new Date(Date.now() + (days * 24 + hours) * 3600000).toISOString();
const businessSeed = {
  id: "demo", name: "САЛАВАТСТЕКЛО", timezone: "Europe/Moscow", channel_title: "Контент-студия · демо",
  channel_id: "@salavatsteklo_demo", profile_confirmed: true, profile_version: 1,
  profile: {description: "Демонстрационное пространство САЛАВАТСТЕКЛО. Материалы о стекле, архитектуре и фирменном стиле.",
    tone: "Профессионально, ясно, с опорой на подтверждённые факты",
    constraints: ["Не выдумывать цены, характеристики, сроки и сертификаты.", "Название бренда: САЛАВАТСТЕКЛО."],
    facts: [{text: "Основной фирменный цвет — петроль #196C7F.", source_ids: ["note"]}]},
};
let demoWorkspaces = [{
  business: structuredClone(businessSeed),
  sources: [{id: "note", name: "Фирменный стиль · выдержка из брендбука", kind: "text"},
            {id: "menu", name: "Брендбук от 15.11.2023 · пример источника", kind: "pdf"}],
  posts: [
    {id: "morning", text: "Стекло. Свет. Пространство.\nДемонстрационный черновик о роли стекла в современной архитектуре.", status: "review", art: "pattern-1",
      revision: 1, created_at: sampleDate(0, -2), scheduled_at: null},
    {id: "pastry", text: "В деталях — характер бренда\nПетроль, чистые линии и округлые формы в фирменном стиле САЛАВАТСТЕКЛО.", status: "approved", art: "pattern-2",
      revision: 2, created_at: sampleDate(-1, -4), scheduled_at: sampleDate(1, 2)},
    {id: "filter", text: "Один бренд. Единый стиль.\nДемонстрационный пример публикации с фирменным паттерном.", status: "published", art: "pattern-3",
      revision: 3, created_at: sampleDate(-3, 0), published_at: sampleDate(-2, 0), scheduled_at: sampleDate(-2, 0)},
    {id: "weekend", text: "От материала к решению\nЧерновик рубрики о применении стекла. Факты и характеристики добавим из подтверждённых источников.", status: "review", art: "pattern-2",
      revision: 1, created_at: sampleDate(-4, 0), scheduled_at: null},
    {id: "beans", text: "Вопросы о стекле\nДемонстрационная рубрика: собираем вопросы аудитории для следующих публикаций.", status: "approved", art: "pattern-1",
      revision: 2, created_at: sampleDate(-5, 0), scheduled_at: sampleDate(3, 0)},
    {id: "barista", text: "Пространство для идей\nПример материала в фирменной палитре компании. Публикация в демо имитируется.", status: "published", art: "pattern-3",
      revision: 1, created_at: sampleDate(-6, 0), published_at: sampleDate(-5, 0), scheduled_at: sampleDate(-5, 0)},
  ],
}];
for (const entry of demoWorkspaces) for (const post of entry.posts) Object.assign(post, {
  business_id: entry.business.id, channel_id: entry.business.channel_id, profile_version: 1,
  used_source_ids: ["note"], warnings: [],
});
const restoredDemo = demo ? window.TirazhDemoStore.read() : {data: null, issue: null};
if (restoredDemo.data) demoWorkspaces = restoredDemo.data.workspaces;
const demoJobs = new Map((restoredDemo.data?.jobs || []).map(job => [job.id, job]));
const demoAssets = new Map((restoredDemo.data?.assets || []).map(asset => [asset.id, asset]));
let state = {businesses: [], business: null, sources: [], posts: [], counts: {}};
let view = views.has(location.hash.slice(1)) ? location.hash.slice(1) : "overview";
let businessId = restoredDemo.data?.selectedBusiness || "";
let statusFilter = "";
let content = {items: [], more: false, page: 0};
let contentLayout = "list";
let searchQuery = "";
let calendarMonth = "";
let selectedDay = "";
let calendarData = {items: [], unscheduled_count: 0};
let manualPreviewUrl = null;
let report = null;
let controls = {jobs: [], operations: [], usage: {}};
let postRequest = 0;
let conversation = [];
const demoConversations = new Map(restoredDemo.data?.conversations || []);
let pageRequest = 0;
let stateRequest = 0;
let toastTimer;
let watchedJob = 0;
const imageUrls = new Map();
const imageTypes = new Map();
let themeOverride = null;
let dateFormatter;
let dateTimezone;

function useTheme() {
  const theme = themeOverride || (insideTelegram ? telegram.colorScheme : "light");
  document.body.dataset.theme = theme === "light" ? "light" : "dark";
  const color = getComputedStyle(document.body).getPropertyValue("--app").trim();
  $('meta[name="theme-color"]').content = color;
  if (insideTelegram && telegram.isVersionAtLeast("6.9")) {
    telegram.setHeaderColor(color);
    telegram.setBackgroundColor(color);
  }
  const toggle = $(".theme-button");
  toggle.innerHTML = icon(theme === "light" ? "theme" : "sun");
  toggle.setAttribute("aria-pressed", String(theme === "light"));
}

function syncBackButton() {
  if (!insideTelegram || !telegram.isVersionAtLeast("6.1")) return;
  if (dialog.open || view !== "overview") telegram.BackButton.show();
  else telegram.BackButton.hide();
}

function selectionFeedback() {
  if (insideTelegram && telegram.isVersionAtLeast("6.1")) telegram.HapticFeedback.selectionChanged();
}

function button(label, action, kind = "primary", glyph = "", attrs = "") {
  const write = accessRules.requiresWrite(action);
  if (write && !canWrite()) return "";
  return '<button type="button" class="button ' + kind + '" data-action="' + action + '" ' + (write ? 'data-write ' : '') + attrs + '>'
    + (glyph ? icon(glyph) : "") + safe(label) + '</button>';
}
function currentAccess() { return accessRules.resolve(state.access, demo); }
function canWrite() { return currentAccess().can_write; }
function requireWrite() { accessRules.assertWrite(currentAccess()); }
function applyAccess() {
  const access = currentAccess();
  document.body.dataset.access = access.can_write ? "write" : "read";
  const banner = $("#access-banner");
  banner.hidden = access.can_write;
  $("#access-label").textContent = access.role === "reviewer" ? "Просмотр для жюри" : "Только просмотр";
  accessRules.apply(root, access);
  accessRules.apply(dialogBody, access);
}
function request(url, options = {}) {
  accessRules.assertRequest(currentAccess(), options.method);
  return window.TirazhHTTP.request(url, options);
}
function headline(kicker, title, description, action = "") {
  return '<header class="page-head"><div><p class="eyebrow">' + safe(kicker) + '</p><h1>'
    + safe(title) + '</h1>' + (description ? '<p>' + safe(description) + '</p>' : '')
    + '</div>' + action + '</header>';
}
function statusLabel(value) {
  return '<span class="status ' + (Object.hasOwn(labels, value) ? value : "editing") + '">'
    + safe(labels[value] || "Черновик") + '</span>';
}
function formatDate(value) {
  if (!value) return "Без даты";
  const timezone = state.business?.timezone || "Europe/Moscow";
  if (dateTimezone !== timezone) {
    dateFormatter = new Intl.DateTimeFormat("ru-RU", {
      day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: timezone,
    });
    dateTimezone = timezone;
  }
  return dateFormatter.format(new Date(value));
}
function notify(text) {
  clearTimeout(toastTimer);
  toast.textContent = text;
  toastTimer = setTimeout(() => { toast.textContent = ""; }, 4200);
}
function announceError(error) {
  if (dialog.open && $("#form-error")) $("#form-error").textContent = error.message;
  else notify(error.message);
}
function persistDemo() {
  if (!demo) return;
  const saved = window.TirazhDemoStore.write({workspaces: demoWorkspaces, jobs: [...demoJobs.values()],
    conversations: [...demoConversations], assets: [...demoAssets.values()], selectedBusiness: businessId});
  const note = $("#demo-storage-note");
  if (note) note.textContent = saved ? "Изменения сохраняются в этом браузере." : "Хранилище недоступно или заполнено. Изменения сохраняются только в этой вкладке.";
}
function demoApi(url, method, body) {
  const [pathname, query] = url.split("?");
  const params = new URLSearchParams(query);
  const parts = pathname.split("/").filter(Boolean);
  const selected = demoWorkspaces.find((w) => w.business.id === (params.get("business") || parts[1]))
    || demoWorkspaces[0];
  if (pathname === "/state") {
    const {business, sources, posts} = selected;
    const counts = posts.reduce((result, post) => {
      result[post.status] = (result[post.status] || 0) + 1;
      return result;
    }, {total: posts.length});
    return {access: {role: "owner", can_write: true}, businesses: demoWorkspaces.map(({business: b}) => ({id: b.id, name: b.name})),
      business, sources, counts, posts: posts.slice(0, 6), upcoming: posts.filter(p => p.status === "approved" && p.scheduled_at).sort((a,b) => Date.parse(a.scheduled_at) - Date.parse(b.scheduled_at))[0]};
  }
  if (pathname === "/business" && method === "POST") {
    const business = {id: crypto.randomUUID(), name: body.name, timezone: "Europe/Moscow", profile_version: 0};
    demoWorkspaces.push({business, sources: [], posts: []});
    return business;
  }
  if (parts[0] === "business") {
    const {business, sources, posts} = selected;
    if (parts[2] === "history") return {items: demoConversations.get(business.id) || []};
    if (parts[2] === "chat") {
      const history = demoConversations.get(business.id) || [];
      const answer = "Это демонстрационный ответ. В рабочем режиме AI использует профиль «" + business.name
        + "», факты из материалов и историю этой компании. Для подготовки тестового черновика нажмите «Новое задание».";
      history.push({user_text: body.text, assistant_text: answer, created_at: new Date().toISOString()});
      demoConversations.set(business.id, history);
      return {intent: {action: "answer", answer}};
    }
    if (parts[2] === "control") return {business, jobs: [...demoJobs.values()].filter(j => j.business_id === business.id),
      operations: [], usage: {used_today: [...demoJobs.values()].length, daily_limit: 100, running: 0}, failed_notifications: []};
    if (parts[2] === "jobs") return {items: [...demoJobs.values()].filter(j => j.business_id === business.id)};
    if (parts[2] === "pause") { business.publications_paused = body.paused; return business; }
    if (parts[2] === "automation") {
      Object.assign(business, {automation_enabled: body.enabled, interval_hours: body.interval_hours,
        automation_request: body.request, next_generation_at: body.enabled ? body.first_run_at || sampleDate(1, 0) : null});
      return business;
    }
    if (parts[2] === "calendar") return {month: params.get("month"), timezone: business.timezone,
      items: posts.filter(post => post.scheduled_at && dayKey(post.scheduled_at, business.timezone).startsWith(params.get("month"))),
      unscheduled_count: posts.filter(post => !post.scheduled_at).length};
    if (parts[2] === "draft") {
      if (!business.profile_confirmed || !business.channel_id) throw new Error("Подтвердите профиль и подключите канал.");
      const asset = demoAssets.get(body.image_asset_id);
      if (!asset || asset.business_id !== business.id) throw new Error("Добавьте изображение этой компании.");
      const post = {id: crypto.randomUUID(), business_id: business.id, channel_id: business.channel_id,
        profile_version: business.profile_version, used_source_ids: body.source_ids || [], warnings: ["Создано вручную: проверьте факты перед публикацией."],
        text: body.text, image_asset_id: body.image_asset_id, revision: 1, status: "review", is_demo: true,
        created_at: new Date().toISOString(), scheduled_at: body.scheduled_at || null, content_origin: "manual"};
      posts.unshift(post); return post;
    }
    if (parts[2] === "posts") {
      let filtered = params.get("status") ? posts.filter((p) => p.status === params.get("status")) : posts;
      if (params.get("search")) filtered = filtered.filter(post => post.text.toLocaleLowerCase("ru").includes(params.get("search").toLocaleLowerCase("ru")));
      const start = Number(params.get("page") || 0) * 12;
      return {items: filtered.slice(start, start + 12), more: filtered.length > start + 12, total: filtered.length};
    }
    if (parts[2] === "source") {
      sources.push({id: crypto.randomUUID(), name: body.kind === "url" ? body.value : "Описание бизнеса",
        kind: body.kind});
      if (business.profile) business.profile_confirmed = false;
      return {};
    }
    if (parts[2] === "channel") {
      business.channel_id = body.value;
      business.channel_title = body.value;
      return business;
    }
    if (parts[2] === "profile") {
      if (parts[3] === "manual") {
        if (body.revision !== business.profile_version) throw new Error("Обновите профиль и попробуйте ещё раз.");
        business.profile = {description: body.description, tone: body.tone || "Понятно и по делу", products: body.products || [],
          audience: body.audience || "", constraints: body.constraints || [], facts: [{text: body.description}]};
        business.profile_version++; business.profile_confirmed = false;
        sources.push({id: crypto.randomUUID(), name: "Сведения владельца", kind: "text"});
      } else if (parts[3] === "analyze") {
        if (!sources.length) throw new Error("Добавьте источник.");
        business.profile = {description: "Демонстрационный профиль", tone: "Дружелюбно", facts: []};
        business.profile_version++;
        business.profile_confirmed = false;
      } else business.profile_confirmed = true;
      return business;
    }
    if (parts[2] === "report") {
      const series = [0, 1, 1, 2, 0, 1, 2].map((created, index) => ({
        label: new Intl.DateTimeFormat("ru-RU", {day: "2-digit", month: "2-digit"}).format(
          new Date(Date.now() - (6 - index) * 86400000)),
        created: business.id === "demo" ? created : 0,
        sent: business.id === "demo" && index === 4 ? 1 : 0,
      }));
      return {series, detail: "Демонстрационные показатели. Данные Telegram не запрашиваются."};
    }
    if (parts[2] === "prepare") {
      const id = crypto.randomUUID();
      if (!business.profile_confirmed || !business.channel_id) throw new Error("Сначала подтвердите профиль и подключите канал.");
      posts.unshift({id, business_id: business.id, channel_id: business.channel_id, profile_version: business.profile_version,
        used_source_ids: sources.map(s => s.id), warnings: [], text: "Демонстрационный черновик\n" + body.request,
        status: "review", art: "pattern-1", revision: 1, created_at: new Date().toISOString()});
      demoJobs.set(id, {id, business_id: business.id, request: body.request, status: "done", stage: "done",
        processed: 1, total: 1, created_at: new Date().toISOString()});
      return demoJobs.get(id);
    }
  }
  if (parts[0] === "job") {
    const job = demoJobs.get(parts[1]);
    if (!job) throw new Error("Задание не найдено.");
    if (parts[2] === "cancel") { job.status = "cancelled"; job.stage = "cancelled"; }
    return job;
  }
  if (parts[0] === "post") {
    const post = demoWorkspaces.flatMap((w) => w.posts).find((p) => p.id === parts[1]);
    if (!post) throw new Error("Пост не найден.");
    if (method === "POST") {
      if (body.revision !== post.revision) throw new Error("Откройте актуальную версию.");
      if (parts[2] === "edit") {
        if (body.scheduled_at && new Date(body.scheduled_at) <= new Date()) {
          throw new Error("Выберите время в будущем.");
        }
        Object.assign(post, body, {revision: post.revision + 1, status: "review"});
      }
      if (parts[2] === "revise-text" || parts[2] === "revise-image") {
        if (parts[2] === "revise-text") post.text += "\nДемо-правка: " + body.feedback;
        else {
          post.art = post.art === "pattern-1" ? "pattern-2" : "pattern-1";
          delete post.image_asset_id;
        }
        post.revision++; post.status = "review";
      }
      if (parts[2] === "refresh-context") {
        post.profile_version = demoWorkspaces.find(w => w.business.id === post.business_id).business.profile_version;
        post.revision++; post.status = "review";
      }
      if (parts[2] === "approve") {
        if (!post.scheduled_at) throw new Error("Сначала укажите время.");
        post.status = "approved";
      }
      if (parts[2] === "reject") post.status = "rejected";
    }
    return post;
  }
  throw new Error("Действие недоступно.");
}
async function api(url, body) {
  const method = body ? "POST" : "GET";
  accessRules.assertRequest(currentAccess(), method);
  if (demo) { const result = demoApi(url, method, body); if (method === "POST") persistDemo(); return result; }
  if (!telegram?.initData) throw new Error("Откройте мини-приложение из меню Telegram-бота.");
  const response = await request("./api" + url, {
      method, headers: {"X-Telegram-Init-Data": telegram.initData,
        ...(body ? {"Content-Type": "application/json"} : {})},
      body: body ? JSON.stringify(body) : undefined,
      cache: "no-store",
    });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.error || "Сервис временно недоступен.");
  }
  return response.json();
}
function postArt(post, additionalClass = "") {
  const localImage = demo && demoAssets.get(post.image_asset_id);
  if (localImage && localImage.business_id === post.business_id) return '<div class="post-art ' + additionalClass + '"><img src="' + safe(localImage.data_url) + '" alt="Изображение к посту"></div>';
  const art = ["pattern-1", "pattern-2", "pattern-3"].includes(post.art) ? post.art : "pattern-1";
  if (demo) return '<div class="post-art brand-post-art ' + additionalClass + '"><img src="./brand-' + art
    + '.png" alt="Фирменный паттерн САЛАВАТСТЕКЛО"></div>';
  return '<div class="post-art ' + additionalClass + '" data-asset="' + safe(post.image_asset_id) + '">'
    + icon("image") + '</div>';
}
async function loadImages(container) {
  if (demo || !telegram?.initData) return;
  await Promise.all([...container.querySelectorAll("[data-asset]")].map(loadImage));
}
async function loadImage(node) {
  const id = node?.dataset.asset;
  if (!id || demo || !telegram?.initData) return;
  node.setAttribute("aria-busy", "true");
  node.innerHTML = icon("image");
  if (!imageUrls.has(id)) {
    const pending = request("./api/asset/" + path(id), {
      headers: {"X-Telegram-Init-Data": telegram.initData}, cache: "no-store", timeoutMs: 60000,
    }).then(async (response) => {
      if (!response.ok) throw new Error("Нет изображения");
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const check = new Image(); check.src = url;
      try {
        await check.decode();
        if (!check.naturalWidth || !check.naturalHeight) throw new Error("Неверное изображение");
      } catch (error) { URL.revokeObjectURL(url); throw error; }
      imageTypes.set(id, blob.type);
      return url;
    }).catch(() => null); // Keep failures cached until an explicit image retry.
    imageUrls.set(id, pending);
  }
  try {
    const url = await imageUrls.get(id);
    if (!url) throw new Error("Изображение недоступно");
    const img = new Image(); img.decoding = "async"; img.src = url; img.alt = "Изображение к посту";
    await img.decode();
    if (!node.isConnected || node.dataset.asset !== id) return;
    node.replaceChildren(img);
    if (node.closest("#dialog-body") && img.naturalWidth > 0 && img.naturalHeight > 0) {
      dialogBody.querySelectorAll("[data-requires-image]").forEach(control => {
        if (!accessRules.requiresWrite(control.dataset.action) || canWrite()) control.removeAttribute("disabled");
      });
    }
  } catch {
    if (node.isConnected) {
      node.innerHTML = '<span class="image-error"><span>Не удалось загрузить изображение.</span>'
        + (node.closest("button") ? '' : button("Загрузить изображение", "retry-image", "secondary", "image")) + '</span>';
    }
  } finally { if (node.isConnected) node.setAttribute("aria-busy", "false"); }
}
function postCards(posts) {
  return '<div class="post-list">' + posts.map((post) => {
    const title = post.text.split("\n")[0];
    return '<button class="post-card" data-action="open-post" data-id="' + safe(post.id) + '">'
      + postArt(post) + '<div class="post-body"><h3>' + safe(title.slice(0, 85)) + '</h3>'
      + '<div class="post-meta">' + statusLabel(post.status)
      + '<span class="date">' + safe(formatDate(post.scheduled_at || post.created_at)) + '</span></div>'
      + '</div>' + icon("next") + '</button>';
  }).join("") + '</div>';
}
const editorialSteps = [
  {art: "draft", kicker: "01 / ПОДГОТОВКА", title: "Черновик с AI",
    text: "Опишите тему и формат. После подключения AI студия подготовит текст с учётом профиля компании.",
    action: "new-post", label: "Создать черновик", icon: "plus"},
  {art: "calendar", kicker: "02 / РАСПИСАНИЕ", title: "План публикаций",
    text: "Проверьте черновик, выберите дату и согласуйте версию. Студия опубликует её в назначенное время.",
    action: "filter", status: "approved", label: "Открыть расписание", icon: "calendar"},
  {art: "send", kicker: "03 / ПУБЛИКАЦИЯ", title: "Посты в Telegram",
    text: "Откройте отправленные посты и проверьте, что уже вышло в вашем канале.",
    action: "filter", status: "published", label: "Смотреть публикации", icon: "telegram"},
];
const editorialStatuses = [
  ["review", "На проверке", "clock", "Черновики ждут вашего решения."],
  ["approved", "В расписании", "calendar", "Согласованные посты с датой выхода."],
  ["published", "Опубликовано", "check", "Посты, которые уже вышли в канале."],
];

function pressArt(kind, className = "") {
  const number = {draft: 1, calendar: 2, send: 3}[kind] || 1;
  return '<div class="brand-hero-art ' + className + '" aria-hidden="true"><img src="./brand-pattern-'
    + number + '.png" alt=""></div>';
}
function pressHeading(id, title) {
  return '<h2 class="press-heading" id="' + id + '"><span>' + title + '</span></h2>';
}
function stepButton(step, label = step.label) {
  return button(label, step.action, "outline", step.icon, step.status ? 'data-status="' + step.status + '"' : "");
}
function stepContent(step, compact = false) {
  return pressArt(step.art) + '<div class="step-copy"><p class="press-kicker">' + step.kicker + '</p>'
    + '<h3>' + step.title + '</h3>' + (compact ? "" : '<p class="step-description">' + step.text + '</p>')
    + stepButton(step, compact ? "Открыть" : step.label) + '</div>';
}
function pressCarousel(compact) {
  const label = compact ? "Инструменты редакции" : "Этапы публикации";
  return '<div class="press-carousel ' + (compact ? "spotlight" : "guide") + '" data-slide="0"'
    + (compact ? ' data-compact' : '') + ' role="group" aria-label="' + label + '">'
    + '<div class="slide-surface glass-card" aria-live="polite">' + stepContent(editorialSteps[0], compact) + '</div>'
    + '<div class="slide-controls">'
    + button("Назад", "slide-prev", "slide-arrow", "prev")
    + '<span class="slide-dots">' + editorialSteps.map((step, index) =>
      '<button type="button" class="slide-dot" data-action="slide-to" data-index="' + index
      + '" aria-label="' + step.title + '" aria-pressed="' + (index === 0) + '"></button>').join("") + '</span>'
    + button("Вперёд", "slide-next", "slide-arrow", "next") + '</div></div>';
}
function switchSlide(control, action) {
  const carousel = control.closest(".press-carousel");
  const current = Number(carousel.dataset.slide);
  const count = editorialSteps.length;
  const next = action === "slide-to" ? Number(control.dataset.index)
    : (current + (action === "slide-next" ? 1 : -1) + count) % count;
  if (current === next) return;
  carousel.dataset.slide = next;
  const surface = carousel.querySelector(".slide-surface");
  surface.innerHTML = stepContent(editorialSteps[next], carousel.hasAttribute("data-compact"));
  accessRules.apply(surface, currentAccess());
  carousel.querySelectorAll(".slide-dot").forEach((dot, index) => {
    dot.setAttribute("aria-pressed", String(index === next));
  });
  selectionFeedback();
}
function setupProgress() {
  const b = state.business;
  const steps = [
    [state.sources.length > 0, "Материалы", "add-source", "Добавьте описание, сайт или PDF"],
    [Boolean(b.profile_confirmed), "Профиль", b.profile ? "confirm-profile" : "manual-profile", "Проверьте сведения о компании"],
    [Boolean(b.channel_id), "Канал", "channel", "Подключите тестовый Telegram-канал"],
  ];
  const done = steps.filter(s => s[0]).length;
  return '<section class="setup-panel panel"><div class="section-top"><div><p class="eyebrow">НАСТРОЙКА КОМПАНИИ</p>'
    + '<h2>' + (done === 3 ? "Всё готово к работе" : "Ещё " + (3 - done) + (done === 2 ? " шаг" : " шага") + " до первого поста")
    + '</h2></div><span class="setup-count">' + done + '/3</span></div><div class="setup-steps">'
    + steps.map(([complete, title, action, detail], i) => '<button class="setup-step ' + (complete ? "complete" : "")
      + '" data-action="' + action + '"><span class="setup-number">' + (complete ? icon("check") : i + 1)
      + '</span><span><strong>' + title + '</strong><small>' + (complete ? "Готово" : detail) + '</small></span>'
      + icon("next") + '</button>').join("") + '</div></section>';
}
function overview() {
  const b = state.business;
  const upcoming = state.upcoming;
  const ready = b.profile_confirmed && b.channel_id;
  return '<div class="workspace-home"><section class="workspace-hero"><div><p class="press-kicker">КОНТЕНТ-СТУДИЯ</p>'
    + '<h1>От идеи к публикации.</h1><p class="hero-description">' + safe(b.name) + ' · '
    + (ready ? "Всё для контента в одном пространстве." : "Давайте настроим пространство компании.") + '</p>'
    + '<div class="press-actions">' + button("Создать пост", "manual-post", "primary", "plus")
    + button("Автоподготовка", "automation", "secondary", "spark") + '</div>'
    + '<p class="approval-note">' + icon("check") + 'Каждый пост выходит только после вашего согласования.</p></div>'
    + pressArt("draft", "workspace-illustration") + '</section>'
    + (!ready && canWrite() ? setupProgress() : '')
    + '<div class="dashboard-metrics">' + editorialStatuses.map(([status, label, glyph]) =>
      '<button class="metric-card panel" data-action="filter" data-status="' + status + '"><span class="metric-heading">'
      + icon(glyph) + label + '</span><strong>' + (state.counts[status] || 0) + '</strong>'
      + '<span class="metric-link">Открыть ' + icon("next") + '</span></button>').join("") + '</div>'
    + '<section class="next-publication panel"><span class="press-badge">' + icon(b.publications_paused ? "clock" : "calendar")
    + '</span><div><p class="eyebrow">' + (b.publications_paused ? "ПУБЛИКАЦИИ НА ПАУЗЕ" : "СЛЕДУЮЩАЯ ПУБЛИКАЦИЯ")
    + '</p><strong>' + safe(upcoming?.text.split("\n")[0] || "В расписании пока пусто") + '</strong><p>'
    + (upcoming ? safe(formatDate(upcoming.scheduled_at)) + ' · ' + safe(b.timezone)
      : 'Выберите время и согласуйте готовый черновик.') + '</p></div>'
    + (upcoming ? button("Открыть", "open-post", "secondary", "", 'data-id="' + safe(upcoming.id) + '"')
      : '<button class="text-button" data-view="content">К постам ' + icon("arrow") + '</button>') + '</section>'
    + '<section class="recent-section"><div class="section-top"><div><p class="eyebrow">РЕДАКЦИЯ</p><h2>Последние посты</h2></div>'
    + '<button class="text-button" data-view="content">Все посты ' + icon("arrow") + '</button></div>'
    + (state.posts.length ? '<div class="editorial-grid">' + state.posts.slice(0, 3).map(post =>
      '<button class="editorial-card panel" data-action="open-post" data-id="' + safe(post.id) + '">'
      + postArt(post) + '<div class="editorial-copy">' + statusLabel(post.status) + '<h3>'
      + safe(post.text.split("\n")[0].slice(0, 95)) + '</h3><p>'
      + safe(formatDate(post.scheduled_at || post.created_at)) + '</p></div></button>').join("") + '</div>'
      : '<div class="empty">Здесь появятся тексты и изображения, подготовленные по вашим материалам.</div>') + '</section>'
    + '<section class="assistant-strip panel"><span class="press-badge">' + icon("telegram") + '</span><div>'
    + '<strong>Можно просто написать в Telegram</strong><p>Поставьте задачу своими словами. AI учитывает профиль компании и историю диалога.</p>'
    + '</div><button class="text-button" data-view="work">Задачи ' + icon("arrow") + '</button></section></div>';
}
const jobLabels = {pending: "В очереди", running: "Готовится", done: "Готово", failed: "Ошибка", cancelled: "Отменено"};
const stageLabels = {queued: "Ожидает запуска", plan: "Контент-план", text: "Текст публикации", image: "Создание изображения",
  saving: "Сохранение", done: "Материалы готовы", failed: "Нужна проверка", cancelled: "Отменено"};
function conversationView() {
  return conversation.length ? conversation.map(turn => '<div class="chat-message user"><span class="chat-speaker">Вы</span>'
    + safe(turn.user_text) + '</div><div class="chat-message assistant"><span class="chat-speaker">AI-ассистент</span>'
    + safe(turn.assistant_text || 'Поручение принято.') + '</div>').join('')
    : '<p class="chat-empty">' + (canWrite()
      ? 'Спросите о компании или поручите подготовить пост. Уточняйте задачу в следующих сообщениях — AI сохранит контекст.'
      : 'История диалога пока пуста.') + '</p>';
}
function workView() {
  const b = controls.business || state.business;
  const usage = controls.usage || {};
  const jobs = controls.jobs || [];
  return headline("Управление", "Задачи и расписание", "Регулярная подготовка, очередь и история работы AI.",
    button("Обновить", "refresh", "secondary", "refresh"))
    + '<section class="chat-panel panel"><h2>Диалог с маркетологом</h2><p>Пишите обычным языком. История хранится отдельно для «' + safe(state.business.name) + '».</p>'
    + '<div class="conversation" aria-live="polite">' + conversationView() + '</div>'
    + (!canWrite() ? '<p class="access-note">' + safe(accessRules.reason) + '</p>' : '')
    + '<form class="chat-form" data-chat data-write><textarea name="text" maxlength="4000" required aria-label="Сообщение AI-маркетологу" placeholder="Что мы уже подготовили для компании?"></textarea>'
    + '<button type="submit" class="button primary" aria-label="Отправить сообщение">' + icon("arrow") + '</button></form>'
    + '<p class="form-error" id="chat-error" role="alert"></p></section>'
    + '<div class="control-grid"><section class="panel business-panel"><p class="eyebrow">АВТОНОМНАЯ РАБОТА</p><h2>Автоподготовка</h2><p>'
    + (b.automation_enabled ? 'Каждые ' + b.interval_hours + ' ч · следующий запуск ' + safe(formatDate(b.next_generation_at))
      : 'AI может сам готовить материалы по расписанию. Публикация остаётся за вами.') + '</p>'
    + '<span class="status ' + (b.automation_enabled ? 'published' : 'rejected') + '">'
    + (b.automation_enabled ? 'Включена' : 'Выключена') + '</span><div>'
    + button("Настроить", "automation", "secondary", "clock") + '</div></section>'
    + '<section class="panel business-panel"><p class="eyebrow">КОНТРОЛЬ ОТПРАВКИ</p><h2>'
    + (b.publications_paused ? 'Публикации на паузе' : 'Публикации разрешены') + '</h2><p>'
    + (b.publications_paused ? 'Согласованные материалы не отправляются. Автоподготовка управляется отдельно.'
      : 'В канал отправляются только согласованные версии в назначенное время.') + '</p>'
    + button(b.publications_paused ? "Возобновить" : "Поставить на паузу", "toggle-pause", "secondary", "clock") + '</section></div>'
    + '<section class="usage-strip panel"><span>' + icon("spark") + '<strong>' + (usage.used_today || 0) + ' / '
    + (usage.daily_limit || '—') + '</strong> AI-операций за сегодня</span><small>Общий лимит оператора · сутки UTC. Это число операций, не сумма расходов.</small></section>'
    + '<section class="recent-section"><div class="section-top"><h2>Очередь подготовки</h2>'
    + button("Новое задание", "new-post", "secondary", "plus") + '</div>'
    + (jobs.length ? '<div class="task-list">' + jobs.map(job => '<article class="task-card panel"><div class="task-main"><span class="status '
      + (job.status === 'done' ? 'published' : job.status === 'failed' ? 'failed' : 'review') + '">' + safe(jobLabels[job.status] || job.status)
      + '</span><h3>' + safe(job.request) + '</h3><p>' + safe(stageLabels[job.stage] || job.stage || '')
      + (job.total ? ' · ' + job.processed + '/' + job.total : '') + ' · ' + safe(formatDate(job.created_at)) + '</p>'
      + (job.error ? '<p class="form-error">' + safe(job.error) + '</p>' : '') + '</div>'
      + (job.status === 'pending' ? button("Отменить", "cancel-job", "secondary", "close", 'data-id="' + safe(job.id) + '"') : '')
      + '</article>').join('') + '</div>' : '<div class="empty">Очередь пуста. Создайте задание или включите автоподготовку.</div>') + '</section>'
    + '<details class="panel report-detail activity-panel"><summary>История операций AI</summary>'
    + ((controls.operations || []).length ? '<ol class="activity-list">' + controls.operations.map(op =>
      '<li><strong>' + safe(({interpret: 'Разбор запроса', profile: 'Анализ компании', plan: 'Контент-план', text: 'Подготовка текста', image: 'Подготовка изображения', revise_text: 'Правка текста', revise_image: 'Правка изображения'})[op.kind] || op.kind)
      + '</strong><span>' + safe(jobLabels[op.status] || op.status) + ' · ' + safe(formatDate(op.created_at)) + '</span></li>').join('') + '</ol>'
      : '<p class="dialog-desc">Операции этой компании появятся после обращения к AI.</p>') + '</details>'
    + ((controls.failed_notifications || []).length ? '<div class="notice">Есть недоставленные уведомления: '
      + controls.failed_notifications.length + '. Откройте раздел уведомлений в Telegram-боте для повтора.</div>' : '');
}
function dayKey(value, timezone = state.business?.timezone || "Europe/Moscow") {
  const fields = new Intl.DateTimeFormat("en", {timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit"})
    .formatToParts(new Date(value));
  const part = type => fields.find(field => field.type === type).value;
  return part("year") + "-" + part("month") + "-" + part("day");
}
function calendarItems() {
  return (calendarData.items || []).filter(post => (!statusFilter || post.status === statusFilter)
    && (!searchQuery || post.text.toLocaleLowerCase("ru").includes(searchQuery.toLocaleLowerCase("ru"))));
}
function calendarView() {
  const timezone = calendarData.timezone || state.business.timezone;
  const [year, month] = calendarMonth.split("-").map(Number);
  const first = new Date(Date.UTC(year, month - 1, 1));
  const offset = (first.getUTCDay() + 6) % 7;
  const days = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const today = dayKey(new Date(), timezone);
  const grouped = new Map();
  for (const post of calendarItems()) {
    const key = dayKey(post.scheduled_at, timezone);
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(post);
  }
  const cellCount = Math.ceil((offset + days) / 7) * 7;
  const cells = Array.from({length: cellCount}, (_, index) => {
    const day = index - offset + 1;
    if (day < 1 || day > days) return '<span class="cal-blank" aria-hidden="true"></span>';
    const key = calendarMonth + "-" + String(day).padStart(2, "0");
    const posts = grouped.get(key) || [];
    const dayLabel = new Intl.DateTimeFormat("ru-RU", {day: "numeric", month: "long", year: "numeric", timeZone: "UTC"})
      .format(new Date(Date.UTC(year, month - 1, day)));
    return '<button class="cal-day' + (key === today ? ' is-today' : '') + '" data-action="calendar-day" data-date="' + key
      + '" aria-label="' + dayLabel + ', публикаций: ' + posts.length + '" aria-pressed="' + (key === selectedDay) + '"'
      + (key === today ? ' aria-current="date"' : '') + '><span class="cal-number">' + day + '</span>'
      + (posts.length ? '<span class="cal-count">' + posts.length + '</span><span class="cal-dots" aria-hidden="true">'
        + [...new Set(posts.map(post => post.status))].slice(0, 3).map(status => '<i class="' + status + '"></i>').join('') + '</span>' : '') + '</button>';
  }).join('');
  const selectedPosts = (grouped.get(selectedDay) || []).sort((a, b) => Date.parse(a.scheduled_at) - Date.parse(b.scheduled_at));
  const selectedLabel = new Intl.DateTimeFormat("ru-RU", {day: "numeric", month: "long", timeZone: "UTC"})
    .format(new Date(selectedDay + "T12:00:00Z"));
  const monthLabel = new Intl.DateTimeFormat("ru-RU", {month: "long", year: "numeric", timeZone: "UTC"}).format(first);
  return '<section class="cal-panel panel" aria-label="Календарь публикаций"><div class="cal-header">'
    + '<div><h2 aria-live="polite">' + safe(monthLabel) + '</h2><p>' + safe(timezone) + '</p></div><div class="cal-controls">'
    + button("Сегодня", "calendar-today", "secondary")
    + '<button class="icon-button" data-action="calendar-prev" aria-label="Предыдущий месяц">' + icon("prev") + '</button>'
    + '<button class="icon-button" data-action="calendar-next" aria-label="Следующий месяц">' + icon("next") + '</button></div></div>'
    + '<div class="cal-weekdays" aria-hidden="true">' + ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'].map(day => '<span>' + day + '</span>').join('') + '</div>'
    + '<div class="cal-grid" role="group" aria-label="Дни месяца">' + cells + '</div>'
    + '<div class="cal-legend"><span><i class="approved"></i>Согласовано</span><span><i class="review"></i>На проверке</span><span><i class="published"></i>Опубликовано</span></div>'
    + '<p class="cal-footnote">Без даты: ' + (calendarData.unscheduled_count || 0) + '. Такие посты доступны в списке. Время показано в часовом поясе компании.</p></section>'
    + '<section class="cal-agenda" aria-live="polite"><div class="section-top"><div><p class="eyebrow">ВЫБРАННЫЙ ДЕНЬ</p><h2>' + safe(selectedLabel) + '</h2></div>'
    + button("Добавить пост", "manual-post", "secondary", "plus") + '</div>'
    + (selectedPosts.length ? postCards(selectedPosts) : '<div class="cal-empty panel">' + icon("calendar")
      + '<strong>' + (searchQuery || statusFilter ? 'Нет постов с такими условиями' : 'Этот день свободен') + '</strong><p>'
      + (searchQuery || statusFilter ? 'Измените запрос или сбросьте фильтры.' : 'Создайте пост и укажите время, чтобы он появился в календаре.') + '</p>'
      + (searchQuery || statusFilter ? button("Сбросить фильтры", "clear-search", "secondary") : '') + '</div>') + '</section>';
}
function contentView() {
  const filters = [["", "Все"], ["review", "На согласовании"], ["approved", "Запланированы"], ["published", "Опубликованы"], ["failed", "Ошибки"], ["rejected", "Отклонены"], ["delivery_unknown", "Проверить доставку"]];
  return headline("Редакция", "Посты", "Ваши материалы и календарь публикаций.",
    '<div class="content-create-actions">' + button("Создать", "manual-post", "primary", "plus") + button("С AI", "new-post", "secondary", "spark") + '</div>')
    + '<div class="content-toolbar"><form class="content-search" data-search><input name="search" type="search" maxlength="200" value="' + safe(searchQuery)
    + '" aria-label="Поиск по тексту постов" placeholder="Найти пост по тексту…"><button type="submit" class="button secondary">Найти</button></form>'
    + '<div class="content-layout" aria-label="Отображение постов">'
    + ['list', 'calendar'].map(layout => '<button class="button ' + (contentLayout === layout ? 'primary' : 'secondary') + '" data-action="content-layout" data-layout="' + layout
      + '" aria-pressed="' + (contentLayout === layout) + '">' + icon(layout === 'list' ? 'posts' : 'calendar') + (layout === 'list' ? 'Список' : 'Календарь') + '</button>').join('') + '</div></div>'
    + '<div class="filters" aria-label="Статус постов">' + filters.map(([value, label]) =>
      '<button class="filter" data-action="filter" data-status="' + value + '" aria-pressed="'
      + (value === statusFilter) + '">' + label + '</button>').join("") + '</div>'
    + (contentLayout === "calendar" ? calendarView() : '<p class="content-result" aria-live="polite">'
      + (typeof content.total === 'number' ? 'Найдено: ' + content.total : 'Показано: ' + content.items.length)
      + (searchQuery ? ' · «' + safe(searchQuery) + '»' : '') + '</p>'
      + (content.items.length ? postCards(content.items) : '<div class="cal-empty panel">' + icon("posts") + '<strong>'
        + (searchQuery || statusFilter ? 'Посты не найдены' : 'Начните с первого поста') + '</strong><p>'
        + (searchQuery || statusFilter ? 'Попробуйте другой текст или сбросьте фильтры.' : 'Напишите текст и загрузите изображение — AI для этого не нужен.') + '</p>'
        + button(searchQuery || statusFilter ? "Сбросить фильтры" : "Создать пост", searchQuery || statusFilter ? "clear-search" : "manual-post", "secondary", "plus") + '</div>')
      + (content.more ? '<div class="pager">' + button("Показать ещё", "more", "secondary") + '</div>' : ""));
}
function reportView() {
  const series = report?.series || [];
  const max = Math.max(1, ...series.map((day) => Math.max(day.created, day.sent)));
  const bars = series.map((day, index) => {
    const x = 52 + index * 74;
    const a = Math.round(134 * day.created / max);
    const b = Math.round(134 * day.sent / max);
    return '<g><title>' + safe(day.label) + ': подготовлено ' + day.created + ', опубликовано ' + day.sent
      + '</title><rect x="' + x + '" y="' + (162 - a) + '" width="16" height="' + a + '" rx="4" class="created"/>'
      + '<rect x="' + (x + 20) + '" y="' + (162 - b) + '" width="16" height="' + b
      + '" rx="4" class="sent"/><text x="' + (x + 18) + '" y="183" text-anchor="middle">'
      + safe(day.label) + '</text></g>';
  }).join("");
  return headline("Результаты", "Отчёты", "Подготовка и публикации за последние семь дней.")
    + '<section class="panel chart"><h2>Активность по дням</h2><p>'
    + (demo ? "Демонстрационные данные" : "Данные из истории этого бизнеса") + '</p>'
    + '<svg viewBox="0 0 600 190" role="img" aria-label="График подготовки и публикаций" font-size="15">'
    + [28, 73, 118, 162].map((y) => '<path d="M36 ' + y + 'h530" class="baseline"/>').join("")
    + bars + '</svg><div class="legend"><span><i></i>Подготовлено</span><span><i></i>Опубликовано</span></div></section>'
    + (report?.detail ? '<details class="panel report-detail"><summary>Полный отчёт</summary><pre>'
      + safe(report.detail) + '</pre></details>' : "");
}
function businessView() {
  const b = state.business;
  const profile = b.profile;
  return headline("Настройки", b.name, "Источники, профиль и Telegram-канал.",
    button("Новый бизнес", "new-business", "secondary", "plus"))
    + '<div class="business-layout"><section class="panel business-panel"><h2>Профиль</h2>'
    + '<p>' + safe(profile?.description || "Добавьте материалы и соберите профиль бизнеса.") + '</p>'
    + (profile ? '<p><strong>Стиль:</strong> ' + safe(profile.tone) + '</p><p>'
      + (b.profile_confirmed ? 'Профиль подтверждён.' : 'Профиль ждёт подтверждения.') + '</p><ul>'
      + (profile.facts || []).map((fact) => '<li>' + safe(fact.text) + '</li>').join("") + '</ul>' : "")
    + button("Заполнить вручную", "manual-profile", "secondary", "posts")
    + button("Собрать с AI", "analyze", "secondary", "spark")
    + (profile && !b.profile_confirmed ? button("Подтвердить", "confirm-profile", "primary", "check") : "")
    + '</section><div><section class="panel business-panel"><h2>Источники</h2><p>Описание, публичная страница сайта или PDF с текстовым слоем.</p><ul>'
    + state.sources.map((s) => '<li>' + safe(s.name) + '</li>').join("") + '</ul>'
    + button("Добавить источник", "add-source", "secondary", "plus")
    + button("Загрузить PDF", "upload-pdf", "secondary", "posts")
    + '</section><section class="panel business-panel"><h2>Канал</h2><p>'
    + safe(b.channel_title || b.channel_id || "Канал пока не подключён.") + '</p>'
    + button(b.channel_id ? "Изменить канал" : "Подключить канал", "channel", "secondary", "telegram")
    + '</section></div></div>';
}
function render() {
  const b = state.business;
  workspace.replaceChildren(...state.businesses.map((entry) => new Option(entry.name, entry.id)));
  workspace.disabled = !b;
  if (!b) workspace.append(new Option("Нет бизнеса", ""));
  else workspace.value = b.id;
  document.querySelectorAll("nav [data-view]").forEach((button) => {
    if (button.dataset.view === view) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  root.setAttribute("aria-busy", "false");
  delete root.dataset.loadingBusiness;
  const nextScreen = b ? view : "empty";
  const screenChanged = root.dataset.screen !== nextScreen;
  root.dataset.screen = nextScreen;
  syncBackButton();
  if (!b) {
    root.innerHTML = !canWrite() ? headline("Рабочее пространство", "Пока нет материалов", "Команда ещё не добавила компанию для просмотра.")
      : headline("Первый шаг", "Создайте бизнес", "Добавьте карточку, чтобы начать работу с контентом.")
      + '<div class="empty">Понадобятся описание бизнеса и канал Telegram.<br>'
      + button("Создать бизнес", "new-business", "primary", "plus") + '</div>';
  } else {
    root.innerHTML = {overview, content: contentView, work: workView, reports: reportView, business: businessView}[view]();
  }
  applyAccess();
  if (screenChanged && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    root.animate?.([{opacity: .65, transform: "translateY(5px)"}, {opacity: 1, transform: "none"}],
      {duration: 210, easing: "ease-out"});
  }
  void loadImages(root);
}
function postQuery(page) {
  return new URLSearchParams({page: String(page), ...(statusFilter ? {status: statusFilter} : {}), ...(searchQuery ? {search: searchQuery} : {})});
}
async function loadPage() {
  const epoch = ++pageRequest;
  const id = businessId;
  const screen = view;
  const current = () => epoch === pageRequest && id === businessId && screen === view;
  if (!id) { render(); return true; }
  try {
    if (screen === "content") {
      if (contentLayout === "calendar") {
        if (!calendarMonth) calendarMonth = dayKey(new Date()).slice(0, 7);
        if (!selectedDay || !selectedDay.startsWith(calendarMonth)) selectedDay = calendarMonth + "-01";
        const result = await api("/business/" + path(id) + "/calendar?month=" + path(calendarMonth));
        if (!current()) return false;
        calendarData = result;
      } else {
        const result = await api("/business/" + path(id) + "/posts?" + postQuery(0));
        if (!current()) return false;
        content = {...result, page: 0};
      }
    }
    if (screen === "work") {
      const [result, history] = await Promise.all([api("/business/" + path(id) + "/control"), api("/business/" + path(id) + "/history")]);
      if (!current()) return false;
      controls = result;
      conversation = history.items;
    }
    if (screen === "reports") {
      const result = await api("/business/" + path(id) + "/report");
      if (!current()) return false;
      report = result;
    }
    if (!current()) return false;
    render();
    return true;
  } catch (error) { if (!current()) return false; throw error; }
}
async function refresh() {
  const epoch = ++stateRequest;
  // Invalidate page responses immediately, including while the new state is in flight.
  ++pageRequest;
  const changingBusiness = Boolean(businessId && (businessId !== state.business?.id || root.dataset.loadingBusiness));
  if (changingBusiness) {
    root.dataset.loadingBusiness = businessId;
    root.setAttribute("aria-busy", "true");
    root.innerHTML = '<div class="empty" role="status">Загружаем компанию…</div>';
  }
  try {
    const result = await api("/state" + (businessId ? "?business=" + path(businessId) : ""));
    if (epoch !== stateRequest) return false;
    state = result;
    applyAccess();
    if (!demo && state.runtime_mode === "demo") {
      const banner = $("#mode-banner"); banner.hidden = false;
      banner.innerHTML = '<span class="demo-pill">ДЕМО</span><span>Тестовый backend. AI и публикации имитируются; данные сохраняются.</span>';
    }
    businessId = state.business?.id || "";
    persistDemo();
    return await loadPage();
  } catch (error) {
    if (epoch !== stateRequest) return false;
    if (changingBusiness) {
      root.setAttribute("aria-busy", "false");
      root.innerHTML = '<div class="notice" role="alert">' + safe(error.message) + '</div>'
        + button("Повторить загрузку", "refresh", "primary");
    }
    throw error;
  }
}
async function navigate(next, filter = "") {
  if (!views.has(next)) return;
  view = next;
  statusFilter = filter;
  selectionFeedback();
  history.replaceState(null, "", "#" + next);
  if (!await refresh()) return;
  root.focus({preventScroll: true});
  window.scrollTo({top: 0, behavior: "instant"});
}
function showDialog(html) {
  dialogBody.innerHTML = html;
  accessRules.apply(dialogBody, currentAccess());
  if (!dialog.open) dialog.showModal();
  syncBackButton();
  void loadImages(dialogBody);
}
function dialogHeading(title, detail = "") {
  return '<h2 id="dialog-title">' + safe(title) + '</h2>' + (detail ? '<p>' + safe(detail) + '</p>' : "");
}
function formField(label, name, kind, options = "") {
  const tag = kind === "textarea" ? '<textarea name="' + name + '" required ' + options + '></textarea>'
    : '<input name="' + name + '" type="' + kind + '" required ' + options + '>';
  return '<label class="form-row"><span>' + safe(label) + '</span>' + tag + '</label>';
}
function openForm(action, title, detail, fields, submit, id = businessId, revision = 0) {
  requireWrite();
  showDialog(dialogHeading(title, detail) + '<form data-write data-form="' + action + '" data-id="' + safe(id)
    + '" data-revision="' + revision + '">' + fields
    + '<p class="form-error" id="form-error" role="alert"></p><div class="dialog-actions">'
    + '<button class="button primary" type="submit">' + safe(submit) + '</button>'
    + button("Отмена", "close", "secondary") + '</div></form>');
}
function showPost(post) {
  if (post.business_id !== businessId) { notify("Пост относится к другой компании. Переключитесь на неё, чтобы продолжить."); return; }
  const attrs = 'data-id="' + safe(post.id) + '" data-revision="' + post.revision + '"';
  const staleProfile = post.profile_version !== state.business.profile_version;
  const staleChannel = post.channel_id !== state.business.channel_id;
  const canApprove = post.status === "review" && post.scheduled_at && new Date(post.scheduled_at) > new Date()
    && !staleProfile && !staleChannel;
  let actions = "";
  if (editable.has(post.status)) {
    if (canApprove) actions += button("Согласовать версию " + post.revision, "approve", "primary", "check", attrs + (demo ? "" : ' disabled data-requires-image'));
    actions += button("Текст и время", "edit-post", "secondary", "calendar", attrs);
    actions += button("Правка текста AI", "revise-text", "secondary", "spark", attrs);
    actions += button("Заменить изображение", "replace-image", "secondary", "image", attrs);
    actions += button("Правка изображения AI", "revise-image", "secondary", "spark", attrs);
    if (staleProfile) actions += button("Проверил по новому профилю", "refresh-context", "secondary", "check", attrs);
    if (["review", "approved"].includes(post.status)) actions += button("Отклонить", "reject", "secondary danger", "", attrs);
  }
  const text = !canWrite() ? accessRules.reason : canApprove ? "Согласование относится к этой версии: тексту, изображению, каналу и времени."
    : post.status === "review" ? "Выберите будущее время и проверьте материал перед согласованием." : "Любая правка снимает прежнее согласование.";
  const link = /^https:\/\/t\.me\//.test(post.message_url || "")
    ? '<a class="text-button" target="_blank" rel="noopener noreferrer" href="' + safe(post.message_url)
      + '">Открыть публикацию ' + icon("arrow") + '</a>' : "";
  showDialog(dialogHeading("Проверка публикации") + '<div class="publication-target">' + icon("telegram") + '<div><strong>Telegram · '
    + safe(post.channel_id || 'Канал не выбран') + '</strong><span>' + safe(formatDate(post.scheduled_at)) + ' · '
    + safe(state.business.timezone) + '</span></div></div>' + postArt(post, "preview-art") + '<div class="post-details">'
    + statusLabel(post.status) + '<span class="date">Версия ' + post.revision + ' · Профиль v' + (post.profile_version ?? '—')
    + '</span>' + (post.content_origin === 'manual' ? '<span class="date">Создан вручную</span>' : '') + '</div><p class="post-text">' + safe(post.text) + '</p>'
    + (staleProfile ? '<div class="notice approval-warning">Профиль бизнеса изменился. Проверьте факты и обновите контекст перед согласованием.</div>' : '')
    + (staleChannel ? '<div class="notice approval-warning">Канал изменился. Создайте новый черновик для текущего канала.</div>' : '')
    + ((post.warnings || []).length ? '<div class="notice approval-warning"><strong>Перед публикацией</strong><ul>'
      + post.warnings.map(w => '<li>' + safe(w) + '</li>').join('') + '</ul></div>' : '')
    + '<details class="post-sources"><summary>Источники фактов · ' + (post.used_source_ids || []).length + '</summary><ul>'
    + (post.used_source_ids || []).map(id => '<li>' + safe(state.sources.find(s => s.id === id)?.name || id) + '</li>').join('') + '</ul></details>'
    + '<p class="dialog-desc">' + safe(post.last_error || text) + '</p>'
    + (post.status === 'delivery_unknown' ? '<div class="notice">Сначала проверьте канал и уточните доставку в Telegram-боте. Автоматического повтора нет.</div>' : '')
    + link + '<p class="form-error" id="form-error" role="alert"></p><div class="dialog-actions">'
    + button("Скачать изображение", "download-image", "secondary", "image", demo ? "" : 'disabled data-requires-image')
    + actions + '</div>');
}
function editableDate(value) {
  if (!value) return "";
  const when = new Date(value);
  return new Date(when - when.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}
async function watchJob(job, business) {
  const watch = ++watchedJob;
  for (let index = 0; index < 25; index++) {
    if (watch !== watchedJob) return;
    if (job.status === "done") {
      if (businessId === business) await refresh();
      notify("Черновики готовы.");
      return;
    }
    if (job.status === "failed") { notify(job.error || "Подготовка не завершилась."); return; }
    await new Promise((resolve) => setTimeout(resolve, 3500));
    try { job = await api("/job/" + path(job.id)); }
    catch { notify("Проверьте статус подготовки в чате бота."); return; }
  }
  notify("Задание в работе. Бот сообщит о результате.");
}
document.addEventListener("click", async (event) => {
  const control = event.target.closest("[data-view], [data-action]");
  if (!control || control.disabled) return;
  event.preventDefault();
  if (control.dataset.view) {
    try { await navigate(control.dataset.view); } catch (error) { announceError(error); }
    return;
  }
  const {action, id, revision} = control.dataset;
  if (accessRules.requiresWrite(action) && !canWrite()) { notify(accessRules.reason); return; }
  if (action === "close") { dialog.close(); return; }
  if (action === "slide-prev" || action === "slide-next" || action === "slide-to") {
    switchSlide(control, action);
    return;
  }
  if (action === "show-workflow") {
    const heading = $("#press-workflow");
    heading.setAttribute("tabindex", "-1");
    heading.focus({preventScroll: true});
    heading.scrollIntoView();
    return;
  }
  if (action === "theme") {
    themeOverride = document.body.dataset.theme === "light" ? "dark" : "light";
    useTheme();
    selectionFeedback();
    return;
  }
  control.disabled = true;
  try {
    if (action === "content-layout") {
      contentLayout = control.dataset.layout;
      if (contentLayout === "calendar" && !calendarMonth) { selectedDay = dayKey(new Date()); calendarMonth = selectedDay.slice(0, 7); }
      await loadPage();
    } else if (action === "calendar-day") {
      selectedDay = control.dataset.date; render();
      root.querySelector('[data-date="' + selectedDay + '"]')?.focus({preventScroll:true});
    } else if (["calendar-prev", "calendar-next", "calendar-today"].includes(action)) {
      if (action === "calendar-today") { selectedDay = dayKey(new Date()); calendarMonth = selectedDay.slice(0, 7); }
      else {
        const [year, month] = calendarMonth.split("-").map(Number);
        const next = new Date(Date.UTC(year, month - 1 + (action === "calendar-next" ? 1 : -1), 1));
        calendarMonth = next.toISOString().slice(0, 7); selectedDay = calendarMonth + "-01";
      }
      await loadPage();
    } else if (action === "clear-search") {
      searchQuery = ""; statusFilter = ""; await loadPage();
    } else if (action === "reset-demo") {
      showDialog(dialogHeading("Сбросить демо?", "Созданные в этом браузере тестовые компании, посты, изображения и диалоги будут удалены. Откроются исходные примеры.")
        + '<div class="dialog-actions">' + button("Сбросить тестовые данные", "confirm-reset-demo", "primary") + button("Отмена", "close", "secondary") + '</div>');
    } else if (action === "confirm-reset-demo" && demo) {
      if (!window.TirazhDemoStore.clear()) throw new Error("Браузер не разрешает очистить локальное хранилище.");
      location.reload();
    } else if (action === "manual-profile") {
      const b = state.business, profile = b.profile || {};
      openForm("manual-profile", "Профиль без AI", "Заполните сведения о компании. После сохранения проверьте и подтвердите профиль.",
        '<label class="form-row"><span>Что делает компания</span><textarea name="description" required maxlength="2000">' + safe(profile.description || '') + '</textarea></label>'
        + '<label class="form-row"><span>Продукты и услуги — по одному на строку</span><textarea name="products" maxlength="3000">' + safe((profile.products || []).join("\n")) + '</textarea></label>'
        + '<label class="form-row"><span>Для кого</span><input name="audience" maxlength="1000" value="' + safe(profile.audience || '') + '"></label>'
        + '<label class="form-row"><span>Тон общения</span><input name="tone" required maxlength="400" value="' + safe(profile.tone || 'Понятно и по делу') + '"></label>'
        + '<label class="form-row"><span>Правила бренда — до 20, по одному на строку</span><textarea name="constraints" maxlength="10000">' + safe((profile.constraints || []).join("\n")) + '</textarea></label>',
        "Сохранить профиль", b.id, b.profile_version);
    } else if (action === "replace-image") {
      openForm("replace-image", "Заменить изображение", "Выберите своё изображение. Будет создана новая версия поста, требующая согласования.",
        formField("Изображение PNG или JPEG", "image", "file", 'accept="image/png,image/jpeg"')
        + '<div class="manual-image-preview" hidden></div><p class="manual-hint">' + imageUploadHint() + '</p>',
        "Сохранить новую версию", id, Number(revision));
    } else if (action === "manual-post") {
      if (!state.business.profile_confirmed || !state.business.channel_id) {
        await navigate("business"); notify("Заполните и подтвердите профиль, затем подключите канал. Это можно сделать без AI."); return;
      }
      openForm("manual-post", "Новый пост", "Добавьте свой текст и изображение. Пост сохранится черновиком; публикация требует отдельного согласования.",
        formField("Текст публикации", "text", "textarea", 'maxlength="900" placeholder="О чём расскажем аудитории?"')
        + '<span class="manual-text-count" aria-live="polite">0 / 900</span>'
        + formField("Изображение PNG или JPEG", "image", "file", 'accept="image/png,image/jpeg"')
        + '<div class="manual-image-preview" hidden></div><p class="manual-hint">' + imageUploadHint() + '</p>'
        + '<label class="form-row"><span>Дата и время (необязательно)</span><input name="scheduled_at" type="datetime-local"></label>'
        + '<p class="manual-hint">Ввод в часовом поясе устройства. Календарь покажет время в ' + safe(state.business.timezone) + '.</p>', "Сохранить черновик");
    } else if (action === "refresh") {
      if (await refresh()) notify("Данные обновлены.");
    } else if (action === "retry-image") {
      const node = control.closest("[data-asset]");
      imageUrls.delete(node.dataset.asset);
      imageTypes.delete(node.dataset.asset);
      await loadImage(node);
    } else if (action === "download-image") {
      const image = dialogBody.querySelector(".preview-art img");
      if (!image?.src) throw new Error("Дождитесь загрузки изображения.");
      const download = document.createElement("a");
      const mime = imageTypes.get(image.closest("[data-asset]")?.dataset.asset);
      download.href = image.src;
      download.download = mime === "image/jpeg" || image.src.startsWith("data:image/jpeg") ? "post-image.jpg" : "post-image.png";
      document.body.append(download); download.click(); download.remove();
    } else if (action === "automation") {
      const b = state.business;
      openForm("automation", "Автоподготовка", "AI готовит материалы по расписанию. Каждый готовый пост ждёт отдельного согласования.",
        '<label class="checkbox-row"><input type="checkbox" name="enabled" ' + (b.automation_enabled ? 'checked' : '') + '> Включить автоподготовку</label>'
        + '<label class="form-row"><span>Поручение</span><textarea name="request" required maxlength="4000">'
        + safe(b.automation_request || 'Подготовь один полезный пост о бизнесе') + '</textarea></label>'
        + formField("Интервал, часов", "interval_hours", "number", 'min="1" max="720" value="' + (b.interval_hours || 24) + '"')
        + '<label class="form-row"><span>Первый запуск (часовой пояс устройства)</span><input name="first_run_at" type="datetime-local" value="'
        + editableDate(b.next_generation_at && new Date(b.next_generation_at) > new Date() ? b.next_generation_at : sampleDate(0, 1)) + '"></label>', "Сохранить расписание");
    } else if (action === "toggle-pause") {
      await api("/business/" + path(businessId) + "/pause", {paused: !state.business.publications_paused});
      await refresh(); notify(state.business.publications_paused ? "Отправка публикаций приостановлена." : "Публикации возобновлены.");
    } else if (action === "cancel-job") {
      await api("/job/" + path(id) + "/cancel", {}); await refresh(); notify("Задание отменено.");
    } else if (action === "upload-pdf") {
      openForm("pdf", "Загрузить PDF", "Используйте PDF с текстовым слоем, до 10 МБ. После загрузки соберите и подтвердите профиль.",
        formField("Документ", "file", "file", 'accept="application/pdf,.pdf"'), "Загрузить");
    } else if (action === "revise-text" || action === "revise-image") {
      openForm(action, action === "revise-text" ? "Доработать текст с AI" : "Доработать изображение",
        "Опишите изменения. Прежнее согласование сразу снимается; результат появится в новой версии.",
        formField("Комментарий для AI", "feedback", "textarea", 'maxlength="4000" placeholder="Например: сделай тон спокойнее, сохрани факты"'),
        "Подготовить новую версию", id, Number(revision));
    } else if (action === "refresh-context") {
      showPost(await api("/post/" + path(id) + "/refresh-context", {revision: Number(revision)}));
      await refresh();
    } else if (action === "new-post") {
      if (state.capabilities?.text_generation === "unconfigured" || state.capabilities?.image_generation === "unconfigured") {
        showDialog(dialogHeading("AI пока не подключён", "Модель можно подключить позже. Уже сейчас доступны ручные тексты, загрузка изображений, согласование и календарь.")
          + '<div class="dialog-actions">' + button("Создать вручную", "manual-post", "primary", "plus") + button("Закрыть", "close", "secondary") + '</div>'); return;
      }
      if (!state.business.profile_confirmed || !state.business.channel_id) {
        await navigate("business"); notify("Подтвердите профиль и подключите канал, чтобы создать первый пост."); return;
      }
      openForm("prepare", "Новый черновик", "Опишите тему, формат и количество постов.",
        formField("Задание для AI", "request", "textarea", 'maxlength="4000" placeholder="Например: два поста о стекле в архитектуре"'),
        "Поставить в очередь");
    } else if (action === "new-business") {
      openForm("business", "Новый бизнес", "Укажите название. Материалы можно добавить после создания.",
        formField("Название", "name", "text", 'maxlength="150" placeholder="Название компании"'), "Создать бизнес");
    } else if (action === "add-source") {
      openForm("source", "Добавить источник", "Используйте описание бизнеса или конечный URL публичной страницы.",
        '<label class="form-row"><span>Тип источника</span><select name="kind"><option value="text">Описание</option>'
        + '<option value="url">Страница сайта</option></select></label>'
        + formField("Материал", "value", "textarea", 'maxlength="16000"'), "Сохранить источник");
    } else if (action === "channel") {
      openForm("channel", "Подключить канал", "Добавьте бота администратором канала с правом публикации.",
        formField("Канал", "value", "text", 'maxlength="128" placeholder="@channel или -100…"'), "Проверить канал");
    } else if (action === "analyze" || action === "confirm-profile") {
      if (action === "analyze" && state.capabilities?.text_generation === "unconfigured") {
        showDialog(dialogHeading("AI пока не подключён", "Заполните профиль вручную — это откроет работу с черновиками без обращения к модели.")
          + '<div class="dialog-actions">' + button("Заполнить профиль", "manual-profile", "primary") + button("Закрыть", "close", "secondary") + '</div>'); return;
      }
      if (action === "analyze") notify("AI изучает материалы. Это может занять до двух минут.");
      await api("/business/" + path(businessId) + "/profile/" + (action === "analyze" ? "analyze" : "confirm"),
        action === "analyze" ? {request_id: crypto.randomUUID()} : {revision: state.business.profile_version});
      await refresh();
      notify(action === "analyze" ? "Профиль готов к проверке." : "Профиль подтверждён.");
    } else if (action === "filter") {
      await navigate("content", control.dataset.status);
    } else if (action === "more") {
      const epoch = pageRequest;
      const next = content.page + 1;
      const result = await api("/business/" + path(businessId) + "/posts?" + postQuery(next));
      if (epoch !== pageRequest) return;
      content = {...result, items: content.items.concat(result.items), page: next};
      render();
    } else if (action === "open-post") {
      const epoch = ++postRequest;
      const post = await api("/post/" + path(id));
      if (epoch === postRequest && post.business_id === businessId) showPost(post);
    } else if (action === "edit-post") {
      const epoch = ++postRequest;
      const editingBusiness = businessId;
      const post = await api("/post/" + path(id));
      if (epoch !== postRequest || businessId !== editingBusiness || post.business_id !== editingBusiness) return;
      openForm("edit", "Текст и время", "Время указывается в часовом поясе вашего устройства. Правка создаёт новую версию.",
        '<label class="form-row"><span>Текст</span><textarea name="text" required maxlength="900">'
        + safe(post.text) + '</textarea></label>'
        + '<label class="form-row"><span>' + (post.scheduled_at ? 'Дата и время' : 'Дата и время (необязательно)')
        + '</span><input name="scheduled_at" type="datetime-local" ' + (post.scheduled_at ? 'required ' : '')
        + 'value="' + editableDate(post.scheduled_at) + '"></label>',
        "Сохранить версию", id, post.revision);
    } else if (action === "approve" || action === "reject") {
      const post = await api("/post/" + path(id) + "/" + action, {revision: Number(revision)});
      showPost(post);
      await refresh();
      notify(action === "approve" ? "Версия согласована и поставлена в расписание." : "Пост отклонён.");
    }
  } catch (error) { announceError(error); }
  finally { control.disabled = false; accessRules.apply(root, currentAccess()); accessRules.apply(dialogBody, currentAccess()); }
});
dialog.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!canWrite()) { announceError(new Error(accessRules.reason)); return; }
  const form = event.target;
  const submit = form.querySelector('[type="submit"]');
  if (submit.disabled) return;
  submit.disabled = true;
  const submitLabel = submit.textContent;
  submit.textContent = "Выполняется…";
  const values = Object.fromEntries(new FormData(form));
  const fieldStates = [...form.elements].filter(field => field !== submit).map(field => [field, field.disabled]);
  for (const [field] of fieldStates) field.disabled = true;
  const {form: action, id, revision} = form.dataset;
  const submittingBusiness = businessId;
  const currentForm = () => form.isConnected && dialog.open && businessId === submittingBusiness;
  try {
    if (action === "manual-profile") {
      await api("/business/" + path(id) + "/profile/manual", {revision: Number(revision), description: values.description,
        products: profileLines(values.products, 19, "продуктов"), audience: values.audience, tone: values.tone,
        constraints: profileLines(values.constraints, 20, "правил бренда")});
      if (currentForm()) dialog.close(); await refresh(); notify("Профиль сохранён. Проверьте сведения и подтвердите его.");
    } else if (action === "manual-post") {
      const requestId = form.dataset.requestId || (form.dataset.requestId = crypto.randomUUID());
      const assetId = await uploadManualImage(values.image, id, form);
      const payload = {text: values.text, image_asset_id: assetId, request_id: requestId};
      if (values.scheduled_at) payload.scheduled_at = new Date(values.scheduled_at).toISOString();
      const post = await api("/business/" + path(id) + "/draft", payload);
      if (currentForm()) showPost(post); await refresh(); notify("Черновик сохранён. Он не согласован к публикации.");
    } else if (action === "replace-image") {
      const post = await api("/post/" + path(id));
      const assetId = await uploadManualImage(values.image, post.business_id, form);
      const updated = await api("/post/" + path(id) + "/edit", {revision: Number(revision), image_asset_id: assetId});
      if (currentForm()) showPost(updated); await refresh(); notify("Изображение обновлено. Требуется новое согласование.");
    } else if (action === "automation") {
      const payload = {enabled: values.enabled === "on", interval_hours: Number(values.interval_hours), request: values.request};
      if (values.first_run_at) payload.first_run_at = new Date(values.first_run_at).toISOString();
      await api("/business/" + path(id) + "/automation", payload); if (currentForm()) dialog.close(); await refresh(); notify("Расписание сохранено.");
    } else if (action === "revise-text" || action === "revise-image") {
      const requestId = form.dataset.requestId || (form.dataset.requestId = crypto.randomUUID());
      const post = await api("/post/" + path(id) + "/" + action, {revision: Number(revision), feedback: values.feedback, request_id: requestId});
      if (currentForm()) showPost(post); await refresh(); notify("Новая версия готова. Проверьте и согласуйте её отдельно.");
    } else if (action === "pdf") {
      const file = values.file;
      if (!file?.size || file.size > 10000000) throw new Error("Выберите PDF размером до 10 МБ.");
      if (demo) {
        demoWorkspaces.find(w => w.business.id === id).sources.push({id: crypto.randomUUID(), name: file.name, kind: "pdf"});
        demoWorkspaces.find(w => w.business.id === id).business.profile_confirmed = false;
        persistDemo();
      } else {
        const data = new FormData(); data.append("file", file);
        const response = await request("./api/business/" + path(id) + "/pdf", {method: "POST", headers: {"X-Telegram-Init-Data": telegram.initData}, body: data});
        if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || "Не удалось загрузить документ.");
      }
      if (currentForm()) dialog.close(); await refresh(); notify(demo ? "Демонстрационный источник добавлен. Файл не отправлялся." : "PDF добавлен. Обновите профиль бизнеса.");
    } else if (action === "business") {
      const business = await api("/business", values);
      if (currentForm()) {
        dialog.close(); businessId = business.id; view = "business";
      }
      await refresh();
      notify("Бизнес создан.");
    } else if (action === "edit") {
      const edit = {revision: Number(revision), text: values.text};
      if (values.scheduled_at) edit.scheduled_at = new Date(values.scheduled_at).toISOString();
      const post = await api("/post/" + path(id) + "/edit", edit);
      if (currentForm()) showPost(post);
      await refresh();
      notify("Новая версия сохранена.");
    } else if (action === "prepare") {
      const requestId = form.dataset.requestId || (form.dataset.requestId = crypto.randomUUID());
      const job = await api("/business/" + path(id) + "/prepare", {...values, request_id: requestId});
      if (currentForm()) dialog.close();
      notify(demo ? "Демонстрационный черновик создан." : "Задание в очереди.");
      void watchJob(job, id);
    } else {
      await api("/business/" + path(id) + "/" + action, values);
      if (currentForm()) dialog.close();
      await refresh();
      notify(action === "channel" ? "Канал подключён." : "Источник добавлен.");
    }
  } catch (error) { if (currentForm()) announceError(error); else notify(error.message); }
  finally { for (const [field, disabled] of fieldStates) field.disabled = disabled; submit.disabled = false; submit.textContent = submitLabel; accessRules.apply(dialogBody, currentAccess()); }
});
root.addEventListener("submit", async (event) => {
  const form = event.target;
  if (form.matches("[data-search]")) {
    event.preventDefault(); searchQuery = form.elements.search.value.trim();
    try { await loadPage(); } catch (error) { announceError(error); }
    return;
  }
  if (!form.matches("[data-chat]")) return;
  event.preventDefault();
  if (!canWrite()) { announceError(new Error(accessRules.reason)); return; }
  const submit = form.querySelector("button");
  if (submit.disabled) return;
  const text = form.elements.text.value.trim();
  if (!text) return;
  const id = businessId;
  submit.disabled = true; form.elements.text.disabled = true;
  const requestId = form.dataset.requestId || (form.dataset.requestId = crypto.randomUUID());
  const errorNode = $("#chat-error"); errorNode.textContent = "";
  try {
    const result = await api("/business/" + path(id) + "/chat", {text, request_id: requestId});
    if (id !== businessId) return;
    await refresh();
    const scroll = $(".conversation"); if (scroll) scroll.scrollTop = scroll.scrollHeight;
    if (result.job) { notify("Задание создано. Готовые материалы придут на согласование."); void watchJob(result.job, id); }
    if (result.report) showDialog(dialogHeading("Отчёт по компании") + '<pre class="chat-report">' + safe(result.report) + '</pre>');
    if (["revise_text", "revise_image"].includes(result.intent?.action)) {
      if (result.intent.post_id) showPost(await api("/post/" + path(result.intent.post_id)));
      notify("Откройте нужный пост и подтвердите правку отдельной кнопкой.");
    }
  } catch (error) { if (errorNode.isConnected) errorNode.textContent = error.message; else announceError(error); }
  finally { submit.disabled = false; form.elements.text.disabled = false; accessRules.apply(root, currentAccess()); }
});
async function uploadManualImage(file, business, form) {
  requireWrite();
  await validateManualImage(file);
  if (form.dataset.assetId) return form.dataset.assetId;
  let assetId;
  if (demo) {
    assetId = crypto.randomUUID(); demoAssets.set(assetId, {id: assetId, business_id: business, data_url: await fileData(file)});
  } else {
    const data = new FormData(); data.append("file", file);
    const response = await request("./api/business/" + path(business) + "/image", {method: "POST", headers: {"X-Telegram-Init-Data": telegram.initData}, body: data});
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || "Не удалось сохранить изображение.");
    assetId = (await response.json()).id;
  }
  form.dataset.assetId = assetId;
  return assetId;
}
function fileData(file) {
  return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("Не удалось прочитать изображение.")); reader.readAsDataURL(file); });
}
function profileLines(text, maximum, label) {
  const lines = String(text || "").split("\n").map(value => value.trim()).filter(Boolean);
  if (lines.length > maximum || lines.some(value => value.length > 500)) throw new Error("Допускается до " + maximum + " " + label + ", каждый пункт — до 500 символов.");
  return lines;
}
function imageUploadLimit() {
  const configured = state.capabilities?.image_upload?.max_bytes;
  return demo ? 1_000_000 : Number.isSafeInteger(configured) && configured > 0 ? Math.min(configured, 10_000_000) : 10_000_000;
}
function imageUploadHint() {
  const size = (imageUploadLimit() / 1_000_000).toLocaleString("ru-RU", {maximumFractionDigits: 2});
  return (demo ? "Для локального демо — до " : "До ") + size + " МБ. PNG или JPEG, до 4096 × 4096 пикселей.";
}
async function validateManualImage(file) {
  if (!file || !file.size || !["image/png", "image/jpeg"].includes(file.type)) throw new Error("Выберите изображение PNG или JPEG.");
  if (file.size > imageUploadLimit()) throw new Error(imageUploadHint());
  const url = URL.createObjectURL(file);
  try { const image = new Image(); image.src = url; await image.decode(); if (!image.naturalWidth || image.naturalWidth > 4096 || image.naturalHeight > 4096) throw new Error(); }
  catch { throw new Error("Нужен исправный PNG или JPEG размером до 4096 × 4096 пикселей."); } finally { URL.revokeObjectURL(url); }
}
dialog.addEventListener("change", async event => {
  if (event.target.name !== "image") return;
  const form = event.target.form; delete form.dataset.assetId;
  const preview = form.querySelector('.manual-image-preview');
  if (manualPreviewUrl) URL.revokeObjectURL(manualPreviewUrl);
  preview.hidden = true; preview.replaceChildren();
  const file = event.target.files[0];
  try {
    await validateManualImage(file);
    if (!preview.isConnected || event.target.files[0] !== file) return;
    manualPreviewUrl = URL.createObjectURL(file);
    const image = new Image(); image.src = manualPreviewUrl; image.alt = "Предпросмотр выбранного изображения";
    preview.append(image); preview.hidden = false; $("#form-error").textContent = "";
  } catch (error) { if (preview.isConnected && event.target.files[0] === file) announceError(error); }
});
root.addEventListener("focusin", event => {
  if (event.target.matches('.filters .filter:focus-visible')) event.target.scrollIntoView({block:"nearest", inline:"center", behavior:"instant"});
  const target = event.target;
  if (!target.matches("input,textarea,select,button,a,summary") || !target.matches(":focus-visible")) return;
  const reveal = () => {
    if (!target.isConnected || document.activeElement !== target) return;
    const rect = target.getBoundingClientRect();
    const top = Math.max(0, $(".topbar").getBoundingClientRect().bottom) + 8;
    const dock = $(".dock").getBoundingClientRect();
    const bottom = (dock.height ? Math.min(innerHeight, dock.top) : innerHeight) - 8;
    if (rect.bottom > bottom) window.scrollBy({top: Math.min(rect.bottom - bottom, rect.top - top), behavior: "instant"});
    else if (rect.top < top) window.scrollBy({top: rect.top - top, behavior: "instant"});
  };
  reveal();
  requestAnimationFrame(reveal);
});
root.addEventListener("keydown", async event => {
  const day = event.target.closest('[data-action="calendar-day"]');
  const shifts = {ArrowLeft:-1, ArrowRight:1, ArrowUp:-7, ArrowDown:7};
  if (!day || !(event.key in shifts)) return;
  event.preventDefault();
  const next = new Date(day.dataset.date + 'T12:00:00Z'); next.setUTCDate(next.getUTCDate() + shifts[event.key]);
  selectedDay = next.toISOString().slice(0,10);
  if (!selectedDay.startsWith(calendarMonth)) { calendarMonth = selectedDay.slice(0,7); await loadPage(); } else render();
  root.querySelector('[data-date="' + selectedDay + '"]')?.focus({preventScroll:true});
});
document.addEventListener("input", event => {
  const form = event.target.closest("form");
  if (event.target.name === "text" && form?.dataset.form === "manual-post") form.querySelector('.manual-text-count').textContent = event.target.value.length + " / 900";
  if (form && !form.querySelector('button[type="submit"]')?.disabled) delete form.dataset.requestId;
});
workspace.addEventListener("change", async () => {
  ++postRequest;
  dialog.close();
  businessId = workspace.value;
  searchQuery = ""; calendarMonth = ""; selectedDay = ""; persistDemo();
  try { await refresh(); } catch (error) { announceError(error); }
});
window.addEventListener("pagehide", () => {
  for (const pending of imageUrls.values()) void pending.then((url) => { if (url) URL.revokeObjectURL(url); });
  imageUrls.clear();
  imageTypes.clear();
});
dialog.addEventListener("close", () => { ++postRequest; syncBackButton(); if (manualPreviewUrl) { URL.revokeObjectURL(manualPreviewUrl); manualPreviewUrl = null; } });
if (insideTelegram) {
  telegram.ready();
  telegram.expand();
  telegram.onEvent("themeChanged", () => {
    themeOverride = null;
    useTheme();
  });
  if (telegram.isVersionAtLeast("6.1")) {
    telegram.BackButton.onClick(() => {
      if (dialog.open) dialog.close();
      else void navigate("overview").catch(announceError);
    });
  }
}
if (demo) {
  const banner = $("#mode-banner"); banner.hidden = false;
  banner.innerHTML = '<span class="demo-pill">ДЕМО</span><span>AI, отправка и расписание имитируются. <span id="demo-storage-note">Изменения сохраняются в этом браузере.</span></span>' + button("Сбросить", "reset-demo", "demo-reset");
  if (restoredDemo.issue) notify(restoredDemo.issue);
}
useTheme();
void refresh().catch((error) => {
  root.setAttribute("aria-busy", "false");
  root.innerHTML = headline("САЛАВАТСТЕКЛО", "Контент-студия", "Telegram Mini App")
    + '<div class="notice" role="alert">' + safe(error.message) + '</div>'
    + button("Повторить загрузку", "refresh", "primary");
});
