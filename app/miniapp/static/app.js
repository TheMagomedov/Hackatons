"use strict";

const $ = (selector) => document.querySelector(selector);
const root = $("#main");
const dialog = $("#dialog");
const dialogBody = $("#dialog-body");
const workspace = $("#workspace");
const toast = $("#toast");
const demo = document.body.dataset.mode === "preview";
const telegram = window.Telegram?.WebApp;
const labels = {
  review: "На согласовании", approved: "Запланирован", publishing: "Отправляется",
  published: "Опубликован", failed: "Ошибка", editing: "Редактируется",
  rejected: "Отклонён", delivery_unknown: "Проверьте доставку",
};
const editable = new Set(["review", "approved", "failed", "rejected"]);
const views = new Set(["overview", "content", "reports", "business"]);
const icon = (name) => '<svg class="icon" aria-hidden="true"><use href="#i-' + name + '"></use></svg>';
const safe = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);
const path = encodeURIComponent;
const sampleDate = (days, hours) => new Date(Date.now() + (days * 24 + hours) * 3600000).toISOString();
const businessSeed = {
  id: "demo", name: "Тёплое зерно", timezone: "Europe/Moscow", channel_title: "Тёплое зерно",
  channel_id: "@teploe_zerno", profile_confirmed: true, profile_version: 1,
  profile: {description: "Кофейня с завтраками и фильтр-кофе. Спокойное место для начала дня.",
    tone: "Тепло, просто, без суеты", facts: [{text: "В меню есть завтраки и фильтр-кофе."}]},
};
const demoWorkspaces = [{
  business: structuredClone(businessSeed),
  sources: [{id: "note", name: "Описание кофейни", kind: "text"},
            {id: "menu", name: "Меню завтраков", kind: "pdf"}],
  posts: [
    {id: "morning", text: "Утро начинается здесь\nСырники, тосты и чашка любимого кофе.", status: "review", art: "coffee",
      revision: 1, created_at: sampleDate(0, -2), scheduled_at: null},
    {id: "pastry", text: "Тёплые круассаны\nХрустящая корочка и мягкие слои внутри.", status: "approved", art: "pastry",
      revision: 2, created_at: sampleDate(-1, -4), scheduled_at: sampleDate(1, 2)},
    {id: "filter", text: "Время для фильтр-кофе\nНебольшая пауза, которая меняет день.", status: "published", art: "cup",
      revision: 3, created_at: sampleDate(-3, 0), published_at: sampleDate(-2, 0), scheduled_at: sampleDate(-2, 0)},
  ],
}];
const demoJobs = new Map();
let state = {businesses: [], business: null, sources: [], posts: [], counts: {}};
let view = views.has(location.hash.slice(1)) ? location.hash.slice(1) : "overview";
let businessId = "";
let statusFilter = "";
let content = {items: [], more: false, page: 0};
let report = null;
let pageRequest = 0;
let stateRequest = 0;
let toastTimer;
let watchedJob = 0;
const imageUrls = new Map();

function button(label, action, kind = "primary", glyph = "", attrs = "") {
  return '<button type="button" class="button ' + kind + '" data-action="' + action + '" ' + attrs + '>'
    + (glyph ? icon(glyph) : "") + safe(label) + '</button>';
}
function headline(kicker, title, description, action = "") {
  return '<header class="page-head"><div><p class="eyebrow">' + safe(kicker) + '</p><h1>'
    + safe(title) + '</h1><p>' + safe(description) + '</p></div>' + action + '</header>';
}
function statusLabel(value) {
  return '<span class="status ' + (Object.hasOwn(labels, value) ? value : "editing") + '">'
    + safe(labels[value] || "Черновик") + '</span>';
}
function formatDate(value) {
  if (!value) return "Без даты";
  return new Intl.DateTimeFormat("ru-RU", {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
    timeZone: state.business?.timezone || "Europe/Moscow",
  }).format(new Date(value));
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
    return {businesses: demoWorkspaces.map(({business: b}) => ({id: b.id, name: b.name})),
      business, sources, counts, posts: posts.slice(0, 6), upcoming: posts.find((p) => p.status === "approved")};
  }
  if (pathname === "/business" && method === "POST") {
    const business = {id: crypto.randomUUID(), name: body.name, timezone: "Europe/Moscow", profile_version: 0};
    demoWorkspaces.push({business, sources: [], posts: []});
    return business;
  }
  if (parts[0] === "business") {
    const {business, sources, posts} = selected;
    if (parts[2] === "posts") {
      const filtered = params.get("status") ? posts.filter((p) => p.status === params.get("status")) : posts;
      const start = Number(params.get("page") || 0) * 12;
      return {items: filtered.slice(start, start + 12), more: filtered.length > start + 12};
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
      if (parts[3] === "analyze") {
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
      posts.unshift({id, text: body.request, status: "review", art: "coffee", revision: 1,
        created_at: new Date().toISOString()});
      demoJobs.set(id, {id, status: "done"});
      return demoJobs.get(id);
    }
  }
  if (parts[0] === "job") return demoJobs.get(parts[1]);
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
  if (demo) return demoApi(url, method, body);
  if (!telegram?.initData) throw new Error("Откройте мини-приложение из меню Telegram-бота.");
  let response;
  try {
    response = await fetch("./api" + url, {
      method, headers: {"X-Telegram-Init-Data": telegram.initData,
        ...(body ? {"Content-Type": "application/json"} : {})},
      body: body ? JSON.stringify(body) : undefined,
      cache: "no-store",
    });
  } catch {
    throw new Error("Нет соединения. Проверьте сеть и повторите.");
  }
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.error || "Сервис временно недоступен.");
  }
  return response.json();
}
function postArt(post, additionalClass = "") {
  const art = ["coffee", "pastry", "cup"].includes(post.art) ? post.art : "coffee";
  if (demo) return '<div class="post-art ' + additionalClass + '"><svg class="sample-art" aria-hidden="true" '
    + 'viewBox="0 0 320 148" preserveAspectRatio="xMidYMid slice"><use href="#art-' + art + '"></use></svg></div>';
  return '<div class="post-art ' + additionalClass + '" data-asset="' + safe(post.image_asset_id) + '">'
    + icon("image") + '</div>';
}
async function loadImages(container) {
  if (demo || !telegram?.initData) return;
  await Promise.all([...container.querySelectorAll("[data-asset]")].map(async (node) => {
    const id = node.dataset.asset;
    if (!id) return;
    try {
      if (!imageUrls.has(id)) {
        imageUrls.set(id, fetch("./api/asset/" + path(id), {
          headers: {"X-Telegram-Init-Data": telegram.initData}, cache: "no-store",
        }).then(async (response) => {
          if (!response.ok) throw new Error("Нет изображения");
          return URL.createObjectURL(await response.blob());
        }).catch(() => { imageUrls.delete(id); return null; }));
      }
      const url = await imageUrls.get(id);
      if (url && node.isConnected) {
        const img = new Image();
        img.src = url;
        img.alt = "Изображение к посту";
        node.replaceChildren(img);
      }
    } catch { /* Keep the placeholder if the image is unavailable. */ }
  }));
}
function postCards(posts) {
  return '<div class="post-grid">' + posts.map((post) => {
    const [title, ...lines] = post.text.split("\n");
    const subtitle = lines.join(" ").trim() || "Версия " + post.revision;
    return '<button class="post-card" data-action="open-post" data-id="' + safe(post.id) + '">'
      + postArt(post) + '<div class="post-body"><div class="post-meta">' + statusLabel(post.status)
      + '<span class="date">' + safe(formatDate(post.scheduled_at || post.created_at)) + '</span></div>'
      + '<h3>' + safe(title.slice(0, 70)) + '</h3><p>' + safe(subtitle.slice(0, 100)) + '</p></div></button>';
  }).join("") + '</div>';
}
function metric(value, label, color, glyph) {
  return '<article class="metric"><span class="bubble ' + color + '">' + icon(glyph)
    + '</span><div><strong>' + value + '</strong><p>' + safe(label) + '</p></div></article>';
}
function overview() {
  const counts = state.counts;
  const review = counts.review || 0;
  const attention = review ? review + ' в очереди' : "Всё проверено";
  return headline("Рабочий стол", "Обзор", "Здесь видно, что готовится, что ждёт решения и что уже вышло.",
    button("Новый пост", "new-post", "primary", "plus"))
    + '<section class="overview-top"><article class="hero"><img class="hero-art" src="./art.svg" alt="">'
    + '<div class="hero-copy"><span class="hero-tag">' + icon("spark") + ' КОНТЕНТ-СТУДИЯ</span>'
    + '<h2>Контент<br>в вашем ритме</h2><p>От идеи до публикации в Telegram. Последнее слово остаётся за вами.</p>'
    + button("Создать черновик", "new-post", "secondary", "arrow") + '</div></article>'
    + '<aside class="panel attention"><span class="bubble amber">' + icon("clock") + '</span>'
    + '<h2>На согласовании</h2><p>' + (review ? safe(attention) + '. Откройте черновик, проверьте текст и время.'
      : 'Черновики появятся здесь, когда будут готовы к проверке.') + '</p>'
    + '<button class="text-button" data-action="review-list">К черновикам ' + icon("arrow") + '</button></aside></section>'
    + '<section class="metrics" aria-label="Сводка по постам">'
    + metric(counts.total || 0, "Всего постов", "lavender", "posts")
    + metric(review, "На согласовании", "amber", "clock")
    + metric(counts.approved || 0, "Запланировано", "blue", "calendar")
    + metric(counts.published || 0, "Опубликовано", "mint", "check") + '</section>'
    + '<div class="section-title"><div><h2>Последние посты</h2><p>Откройте карточку для предпросмотра и правок.</p></div>'
    + '<button class="text-button" data-view="content">Все посты ' + icon("arrow") + '</button></div>'
    + (state.posts.length ? postCards(state.posts) : '<div class="empty">Постов пока нет. Создайте первый черновик.</div>');
}
function contentView() {
  const filters = [["", "Все"], ["review", "На согласовании"], ["approved", "Запланированы"], ["published", "Опубликованы"]];
  return headline("Редакция", "Контент", "Черновики, расписание и опубликованные материалы.",
    button("Новый пост", "new-post", "primary", "plus"))
    + '<div class="filters" aria-label="Статус постов">' + filters.map(([value, label]) =>
      '<button class="filter" data-action="filter" data-status="' + value + '" aria-pressed="'
      + (value === statusFilter) + '">' + label + '</button>').join("") + '</div>'
    + (content.items.length ? postCards(content.items) : '<div class="empty">Постов с таким статусом пока нет.</div>')
    + (content.more ? '<div class="pager">' + button("Показать ещё", "more", "secondary") + '</div>' : "");
}
function reportView() {
  const series = report?.series || [];
  const max = Math.max(1, ...series.map((day) => Math.max(day.created, day.sent)));
  const bars = series.map((day, index) => {
    const x = 52 + index * 74;
    const a = Math.round(134 * day.created / max);
    const b = Math.round(134 * day.sent / max);
    return '<g><title>' + safe(day.label) + ': подготовлено ' + day.created + ', опубликовано ' + day.sent
      + '</title><rect x="' + x + '" y="' + (162 - a) + '" width="16" height="' + a + '" rx="4" fill="#8263ec"/>'
      + '<rect x="' + (x + 20) + '" y="' + (162 - b) + '" width="16" height="' + b
      + '" rx="4" fill="#78bea8"/><text x="' + (x + 18) + '" y="183" text-anchor="middle">'
      + safe(day.label) + '</text></g>';
  }).join("");
  return headline("Результаты", "Отчёты", "Подготовка и публикации за последние семь дней.")
    + '<section class="panel chart"><h2>Активность по дням</h2><p>'
    + (demo ? "Демонстрационные данные" : "Данные из истории этого бизнеса") + '</p>'
    + '<svg viewBox="0 0 600 190" role="img" aria-label="График подготовки и публикаций" font-size="12" fill="#676980">'
    + [28, 73, 118, 162].map((y) => '<path d="M36 ' + y + 'h530" stroke="#ece9f4"/>').join("")
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
    + button("Собрать профиль", "analyze", "secondary", "spark")
    + (profile && !b.profile_confirmed ? button("Подтвердить", "confirm-profile", "primary", "check") : "")
    + '</section><div><section class="panel business-panel"><h2>Источники</h2><p>Текст или публичная страница сайта. '
    + 'PDF можно загрузить через чат бота.</p><ul>'
    + state.sources.map((s) => '<li>' + safe(s.name) + '</li>').join("") + '</ul>'
    + button("Добавить источник", "add-source", "secondary", "plus")
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
  if (!b) {
    root.innerHTML = headline("Первый шаг", "Создайте бизнес", "Добавьте карточку, чтобы начать работу с контентом.")
      + '<div class="empty">Понадобятся описание бизнеса и канал Telegram.<br>'
      + button("Создать бизнес", "new-business", "primary", "plus") + '</div>';
  } else {
    root.innerHTML = {overview, content: contentView, reports: reportView, business: businessView}[view]();
  }
  void loadImages(root);
}
async function loadPage() {
  const epoch = ++pageRequest;
  const id = businessId;
  if (!id) { render(); return; }
  if (view === "content") {
    const filter = statusFilter ? "&status=" + path(statusFilter) : "";
    const result = await api("/business/" + path(id) + "/posts?page=0" + filter);
    if (epoch !== pageRequest) return;
    content = {...result, page: 0};
  }
  if (view === "reports") {
    const result = await api("/business/" + path(id) + "/report");
    if (epoch !== pageRequest) return;
    report = result;
  }
  if (epoch === pageRequest) render();
}
async function refresh() {
  const epoch = ++stateRequest;
  const result = await api("/state" + (businessId ? "?business=" + path(businessId) : ""));
  if (epoch !== stateRequest) return;
  state = result;
  businessId = state.business?.id || "";
  await loadPage();
}
async function navigate(next, filter = "") {
  if (!views.has(next)) return;
  view = next;
  statusFilter = filter;
  history.replaceState(null, "", "#" + next);
  await loadPage();
  root.focus({preventScroll: true});
  window.scrollTo({top: 0, behavior: "instant"});
}
function showDialog(html) {
  dialogBody.innerHTML = html;
  if (!dialog.open) dialog.showModal();
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
  showDialog(dialogHeading(title, detail) + '<form data-form="' + action + '" data-id="' + safe(id)
    + '" data-revision="' + revision + '">' + fields
    + '<p class="form-error" id="form-error" role="alert"></p><div class="dialog-actions">'
    + '<button class="button primary" type="submit">' + safe(submit) + '</button>'
    + button("Отмена", "close", "secondary") + '</div></form>');
}
function showPost(post) {
  const attrs = 'data-id="' + safe(post.id) + '" data-revision="' + post.revision + '"';
  const canApprove = post.status === "review" && post.scheduled_at && new Date(post.scheduled_at) > new Date();
  let actions = "";
  if (editable.has(post.status)) {
    if (canApprove) actions += button("Согласовать версию " + post.revision, "approve", "primary", "check", attrs);
    actions += button("Текст и время", "edit-post", "secondary", "calendar", attrs);
    if (["review", "approved"].includes(post.status)) actions += button("Отклонить", "reject", "secondary danger", "", attrs);
  }
  const text = canApprove ? "Проверьте текст, изображение и время перед согласованием."
    : post.status === "review" ? "Выберите будущее время, чтобы согласовать эту версию." : "";
  const link = /^https:\/\/t\.me\//.test(post.message_url || "")
    ? '<a class="text-button" target="_blank" rel="noopener noreferrer" href="' + safe(post.message_url)
      + '">Открыть публикацию ' + icon("arrow") + '</a>' : "";
  showDialog(dialogHeading("Предпросмотр поста") + postArt(post, "preview-art") + '<div class="post-details">'
    + statusLabel(post.status) + '<span class="date">Версия ' + post.revision + ' · '
    + safe(formatDate(post.scheduled_at)) + '</span></div><p class="post-text">' + safe(post.text)
    + '</p><p class="dialog-desc">' + safe(post.last_error || text) + '</p>' + link
    + '<p class="form-error" id="form-error" role="alert"></p><div class="dialog-actions">' + actions + '</div>');
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
  if (action === "close") { dialog.close(); return; }
  control.disabled = true;
  try {
    if (action === "new-post") {
      openForm("prepare", "Новый черновик", "Опишите тему, формат и количество постов.",
        formField("Задание для AI", "request", "textarea", 'maxlength="4000" placeholder="Например: два поста про завтраки на этой неделе"'),
        "Поставить в очередь");
    } else if (action === "new-business") {
      openForm("business", "Новый бизнес", "Укажите название. Материалы можно добавить после создания.",
        formField("Название", "name", "text", 'maxlength="150" placeholder="Тёплое зерно"'), "Создать бизнес");
    } else if (action === "add-source") {
      openForm("source", "Добавить источник", "Используйте описание бизнеса или конечный URL публичной страницы.",
        '<label class="form-row"><span>Тип источника</span><select name="kind"><option value="text">Описание</option>'
        + '<option value="url">Страница сайта</option></select></label>'
        + formField("Материал", "value", "textarea", 'maxlength="16000"'), "Сохранить источник");
    } else if (action === "channel") {
      openForm("channel", "Подключить канал", "Добавьте бота администратором канала с правом публикации.",
        formField("Канал", "value", "text", 'maxlength="128" placeholder="@channel или -100…"'), "Проверить канал");
    } else if (action === "analyze" || action === "confirm-profile") {
      await api("/business/" + path(businessId) + "/profile/" + (action === "analyze" ? "analyze" : "confirm"),
        action === "analyze" ? {} : {revision: state.business.profile_version});
      await refresh();
      notify(action === "analyze" ? "Профиль готов к проверке." : "Профиль подтверждён.");
    } else if (action === "filter" || action === "review-list") {
      await navigate("content", action === "review-list" ? "review" : control.dataset.status);
    } else if (action === "more") {
      const epoch = pageRequest;
      const next = content.page + 1;
      const filter = statusFilter ? "&status=" + path(statusFilter) : "";
      const result = await api("/business/" + path(businessId) + "/posts?page=" + next + filter);
      if (epoch !== pageRequest) return;
      content = {items: content.items.concat(result.items), more: result.more, page: next};
      render();
    } else if (action === "open-post") {
      showPost(await api("/post/" + path(id)));
    } else if (action === "edit-post") {
      const post = await api("/post/" + path(id));
      openForm("edit", "Текст и время", "Время указывается в часовом поясе вашего устройства. Правка создаёт новую версию.",
        '<label class="form-row"><span>Текст</span><textarea name="text" required maxlength="900">'
        + safe(post.text) + '</textarea></label>'
        + formField("Дата и время", "scheduled_at", "datetime-local",
          'value="' + editableDate(post.scheduled_at) + '"'),
        "Сохранить версию", id, post.revision);
    } else if (action === "approve" || action === "reject") {
      const post = await api("/post/" + path(id) + "/" + action, {revision: Number(revision)});
      showPost(post);
      await refresh();
      notify(action === "approve" ? "Версия согласована и поставлена в расписание." : "Пост отклонён.");
    }
  } catch (error) { announceError(error); }
  finally { control.disabled = false; }
});
dialog.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const submit = form.querySelector('[type="submit"]');
  if (submit.disabled) return;
  submit.disabled = true;
  const values = Object.fromEntries(new FormData(form));
  const {form: action, id, revision} = form.dataset;
  try {
    if (action === "business") {
      const business = await api("/business", values);
      businessId = business.id;
      view = "business";
      dialog.close();
      await refresh();
      notify("Бизнес создан.");
    } else if (action === "edit") {
      const post = await api("/post/" + path(id) + "/edit", {revision: Number(revision), text: values.text,
        scheduled_at: new Date(values.scheduled_at).toISOString()});
      showPost(post);
      await refresh();
      notify("Новая версия сохранена.");
    } else if (action === "prepare") {
      const requestId = form.dataset.requestId || (form.dataset.requestId = crypto.randomUUID());
      const job = await api("/business/" + path(id) + "/prepare", {...values, request_id: requestId});
      dialog.close();
      notify(job.status === "done" ? "Демонстрационный черновик создан." : "Задание в очереди.");
      void watchJob(job, id);
    } else {
      await api("/business/" + path(id) + "/" + action, values);
      dialog.close();
      await refresh();
      notify(action === "channel" ? "Канал подключён." : "Источник добавлен.");
    }
  } catch (error) { announceError(error); }
  finally { submit.disabled = false; }
});
workspace.addEventListener("change", async () => {
  businessId = workspace.value;
  try { await refresh(); } catch (error) { announceError(error); }
});
window.addEventListener("pagehide", () => {
  for (const pending of imageUrls.values()) void pending.then((url) => { if (url) URL.revokeObjectURL(url); });
  imageUrls.clear();
});
if (demo) {
  $("#mode-pill").innerHTML = "<span></span> Демо";
  $("#mode-pill").title = "Образец: действия остаются в этом браузере";
} else {
  $("#mode-pill").innerHTML = "<span></span> Telegram";
  $("#mode-pill").classList.add("live");
  telegram?.ready();
  telegram?.expand();
  if (telegram?.isVersionAtLeast?.("6.9")) telegram.setHeaderColor("#11152c");
}
void refresh().catch((error) => {
  root.setAttribute("aria-busy", "false");
  root.innerHTML = headline("Пульс", "Рабочее пространство", "Telegram Mini App")
    + '<div class="notice" role="alert">' + safe(error.message) + '</div>';
});
