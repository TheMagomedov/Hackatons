"use strict";

// This store is deliberately inaccessible in the connected Mini App. It never receives
// Telegram initData, tokens, live API responses or files from a live workspace.
window.TirazhDemoStore = (() => {
  const KEY = "salavatsteklo.preview.v1";
  const MAX_BYTES = 4_000_000;
  const preview = () => document.body.dataset.mode === "preview";
  const object = value => value && typeof value === "object" && !Array.isArray(value);
  const string = (value, max, fallback = "") => typeof value === "string" && value.length <= max ? value : fallback;
  const integer = (value, fallback = 0) => Number.isSafeInteger(value) && value >= 0 ? value : fallback;
  const date = value => typeof value === "string" && value.length < 80 && Number.isFinite(Date.parse(value)) ? value : null;
  const list = (value, max) => { if (!Array.isArray(value) || value.length > max) throw new Error("Invalid demo list"); return value; };
  const required = (value, max) => { const result = string(value, max); if (!result) throw new Error("Invalid demo text"); return result; };
  const texts = (value, max = 30, size = 1000) => list(value || [], max).map(item => required(item, size));
  function clean(raw) {
    if (!object(raw) || raw.version !== 1) throw new Error("Unknown demo format");
    const ids = new Set();
    const workspaces = list(raw.workspaces, 12).map(entry => {
      if (!object(entry) || !object(entry.business)) throw new Error("Invalid demo company");
      const b = entry.business;
      const id = required(b.id, 100);
      if (ids.has(id)) throw new Error("Duplicate company");
      ids.add(id);
      let timezone = string(b.timezone, 100, "Europe/Moscow");
      try { new Intl.DateTimeFormat("ru", {timeZone: timezone}); } catch { timezone = "Europe/Moscow"; }
      const profile = object(b.profile) ? {
        description: string(b.profile.description, 2000), tone: string(b.profile.tone, 1000),
        name: string(b.profile.name, 150), products: texts(b.profile.products || []),
        audience: string(b.profile.audience, 1000), constraints: texts(b.profile.constraints || []),
        facts: list(b.profile.facts || [], 30).map(fact => ({text: required(fact.text, 2000), source_ids: texts(fact.source_ids || [], 20, 100)})),
      } : null;
      const sources = list(entry.sources, 40).map(source => ({id: required(source.id, 100), name: required(source.name, 250),
        kind: ["text", "url", "pdf"].includes(source.kind) ? source.kind : "text"}));
      const posts = list(entry.posts, 250).map(post => {
        if (!object(post) || post.business_id !== id) throw new Error("Invalid demo post ownership");
        const status = ["review", "approved", "published", "failed", "rejected", "editing", "publishing", "delivery_unknown"].includes(post.status) ? post.status : "review";
        return {id: required(post.id, 100), business_id: id, channel_id: string(post.channel_id, 128),
          text: required(post.text, 4000), revision: integer(post.revision, 1), status,
          profile_version: integer(post.profile_version), used_source_ids: texts(post.used_source_ids || [], 40, 100),
          warnings: texts(post.warnings || []), art: ["pattern-1", "pattern-2", "pattern-3"].includes(post.art) ? post.art : "pattern-1",
          content_origin: post.content_origin === "manual" ? "manual" : "generated",
          image_asset_id: string(post.image_asset_id, 100), created_at: date(post.created_at) || new Date().toISOString(),
          scheduled_at: date(post.scheduled_at), published_at: date(post.published_at), is_demo: true};
      });
      return {business: {id, name: required(b.name, 150), timezone, profile, profile_version: integer(b.profile_version),
        profile_confirmed: Boolean(profile && b.profile_confirmed === true), channel_id: string(b.channel_id, 128),
        channel_title: string(b.channel_title, 150), automation_enabled: b.automation_enabled === true,
        publications_paused: b.publications_paused === true, interval_hours: Math.max(1, Math.min(720, integer(b.interval_hours, 24))),
        automation_request: string(b.automation_request, 4000, "Подготовь один полезный пост о бизнесе"),
        next_generation_at: date(b.next_generation_at)}, sources, posts};
    });
    if (!workspaces.length) throw new Error("Empty demo store");
    const jobs = list(raw.jobs || [], 150).filter(job => object(job) && ids.has(job.business_id)).map(job => ({
      id: required(job.id, 100), business_id: job.business_id, request: required(job.request, 4000),
      status: ["pending", "running", "done", "failed", "cancelled"].includes(job.status) ? job.status : "failed",
      stage: string(job.stage, 30), processed: integer(job.processed), total: integer(job.total),
      created_at: date(job.created_at) || new Date().toISOString(), error: string(job.error, 1000),
    }));
    const conversations = list(raw.conversations || [], 12).filter(entry => Array.isArray(entry) && ids.has(entry[0]))
      .map(([id, turns]) => [id, list(turns, 50).map(turn => ({user_text: required(turn.user_text, 4000),
        assistant_text: string(turn.assistant_text, 5000), created_at: date(turn.created_at) || new Date().toISOString()}))]);
    const assets = list(raw.assets || [], 50).map(asset => {
      if (!object(asset) || !ids.has(asset.business_id) || !/^data:image\/(png|jpeg);base64,[a-zA-Z0-9+/=]+$/.test(asset.data_url || "")
        || asset.data_url.length > 1_500_000) throw new Error("Invalid demo image");
      return {id: required(asset.id, 100), business_id: asset.business_id, data_url: asset.data_url};
    });
    return {version: 1, workspaces, jobs, conversations, assets,
      selectedBusiness: ids.has(raw.selectedBusiness) ? raw.selectedBusiness : workspaces[0].business.id};
  }
  function read() {
    if (!preview()) return {data: null, issue: null};
    try {
      const value = localStorage.getItem(KEY);
      if (!value) return {data: null, issue: null};
      if (value.length > MAX_BYTES) throw new Error("Oversized demo");
      return {data: clean(JSON.parse(value)), issue: null};
    } catch { return {data: null, issue: "Не удалось восстановить локальное демо. Открыты исходные примеры."}; }
  }
  function write(data) {
    if (!preview()) return false;
    try {
      const encoded = JSON.stringify(clean({version: 1, ...data}));
      if (encoded.length > MAX_BYTES) return false;
      localStorage.setItem(KEY, encoded);
      return true;
    } catch { return false; }
  }
  function clear() {
    if (!preview()) return false;
    try { localStorage.removeItem(KEY); return true; } catch { return false; }
  }
  return Object.freeze({read, write, clear});
})();
