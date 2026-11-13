"use strict";
// Development preview only. The authenticated server never serves this module.
if (document.body.dataset.mode === "preview") window.TirazhPreview = (() => {
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
const restoredDemo = window.TirazhDemoStore.read();
if (restoredDemo.data) demoWorkspaces = restoredDemo.data.workspaces;
const demoJobs = new Map((restoredDemo.data?.jobs || []).map(job => [job.id, job]));
const demoAssets = new Map((restoredDemo.data?.assets || []).map(asset => [asset.id, asset]));
const demoConversations = new Map(restoredDemo.data?.conversations || []);
function api(url, method, body, dayKey) {
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
    if (parts[2] === "knowledge" && parts[3] === "search") return {items: [], preview: true};
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
        else post.art = post.art === "pattern-1" ? "pattern-2" : "pattern-1";
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

return Object.freeze({
  api,
  selectedBusiness: restoredDemo.data?.selectedBusiness || "",
  storageIssue: restoredDemo.issue,
  asset: id => demoAssets.get(id),
  addAsset(asset) { demoAssets.set(asset.id, asset); },
  addPdf(businessId, name) {
    const entry = demoWorkspaces.find(w => w.business.id === businessId);
    if (!entry) throw new Error("Компания не найдена.");
    entry.sources.push({id: crypto.randomUUID(), name, kind: "pdf"});
    entry.business.profile_confirmed = false;
  },
  persist(selectedBusiness) {
    return window.TirazhDemoStore.write({workspaces: demoWorkspaces, jobs: [...demoJobs.values()],
      conversations: [...demoConversations], assets: [...demoAssets.values()], selectedBusiness});
  },
  clear: () => window.TirazhDemoStore.clear(),
});
})();
