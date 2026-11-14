"use strict";

// Telegram's SDK reads the same tgWebAppData fragment. Keep the raw signed value
// in memory only; the server verifies its HMAC, age and workspace role on every API call.
// Reference: https://telegram.org/js/telegram-web-app.js (initParams and postEvent).
window.TirazhSession = (() => {
  const preview = document.body.dataset.mode === "preview";
  const sdkScript = document.querySelector("#telegram-sdk");
  const callbacks = new Set();
  const bounded = value => typeof value === "string" && value.length > 0 && value.length <= 8192 ? value : "";
  const sdk = () => preview ? null : window.Telegram?.WebApp || null;
  let launchData = "";
  let studioLaunch = "", studioSession = "", studioError = "", exchangePromise = null;
  if (!preview) {
    const fragment = String(location.hash || "").slice(1);
    if (fragment.length <= 65536) {
      const query = fragment.includes("?") ? fragment.slice(fragment.indexOf("?") + 1) : fragment;
      const values = new URLSearchParams(query).getAll("tgWebAppData");
      if (values.length === 1) launchData = bounded(values[0]);
      const launches = new URLSearchParams(query).getAll("studioLaunch");
      if (!launchData && launches.length === 1 && /^[A-Za-z0-9_-]{43}$/.test(launches[0])) {
        studioLaunch = launches[0];
        // The login ticket is one-use: remove it before navigation or third-party SDK activity.
        history.replaceState(null, "", "#overview");
      }
    }
  }
  const initData = () => preview ? "" : bounded(sdk()?.initData) || launchData;
  const authenticated = () => Boolean(initData() || studioSession);
  const headers = () => studioSession ? {"X-Studio-Session": studioSession} : {"X-Telegram-Init-Data": initData()};
  const error = () => studioError;
  async function exchangeLaunch() {
    try {
      const response = await window.TirazhHTTP.request("./session/vk", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({launch: studioLaunch}), cache: "no-store", timeoutMs: 20000,
      });
      const result = await response.json();
      if (!response.ok || !/^[A-Za-z0-9_-]{43}$/.test(result.session || "")) {
        throw new Error("Ссылка входа истекла или уже использована. Откройте новую через /menu в боте ВК.");
      }
      studioSession = result.session; // Memory only, never localStorage/sessionStorage/cookies.
    } catch {
      studioError = "Не удалось войти через ВК. Отправьте /menu боту и откройте новую ссылку студии.";
    } finally { studioLaunch = ""; }
  }
  const loaded = () => { for (const callback of callbacks) callback(); };
  sdkScript?.addEventListener("load", loaded);
  sdkScript?.addEventListener("error", loaded);

  function onBridge(callback) {
    callbacks.add(callback);
    if (sdk()) callback();
  }
  async function waitForInitData(timeoutMs = 2500) {
    if (studioLaunch || exchangePromise) {
      exchangePromise ||= exchangeLaunch();
      await exchangePromise;
      return "";
    }
    if (preview || initData() || sdk() || !sdkScript) return initData();
    return new Promise(resolve => {
      const finish = () => {
        clearTimeout(timer);
        callbacks.delete(finish);
        resolve(initData());
      };
      const timer = setTimeout(finish, timeoutMs);
      callbacks.add(finish);
    });
  }
  function ready() {
    if (preview) return;
    try {
      if (sdk()?.ready) { sdk().ready(); return; }
      if (!launchData) return;
      // UI readiness only; never sends user data, chat messages or auth decisions.
      if (typeof window.TelegramWebviewProxy?.postEvent === "function") {
        window.TelegramWebviewProxy.postEvent("web_app_ready", JSON.stringify(""));
      } else if (typeof window.external?.notify === "function") {
        window.external.notify(JSON.stringify({eventType: "web_app_ready", eventData: ""}));
      } else if (window.parent && window.parent !== window) {
        window.parent.postMessage(JSON.stringify({eventType: "web_app_ready", eventData: ""}), "https://web.telegram.org");
      }
    } catch { /* Native bridge features must never prevent rendering the local UI. */ }
  }
  function navigateHash(view) {
    // Preserve Telegram's original launch parameters for a late SDK and page reload.
    if (!launchData) history.replaceState(null, "", "#" + view);
  }
  document.querySelector("#startup-reload")?.addEventListener("click", () => location.reload());
  return Object.freeze({initData, authenticated, headers, error, sdk, onBridge, waitForInitData, ready, navigateHash});
})();
