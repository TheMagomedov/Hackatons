"use strict";

// One request, never an automatic retry: a timed-out mutation may have succeeded.
window.TirazhHTTP = Object.freeze({
  async request(url, options = {}) {
    const {timeoutMs, signal, ...init} = options;
    const delay = timeoutMs ?? ((init.method || "GET").toUpperCase() === "GET" ? 20000 : 150000);
    if (!Number.isFinite(delay) || delay <= 0 || delay > 180000) throw new TypeError("Invalid request timeout");
    const controller = new AbortController();
    let timedOut = false;
    const cancel = () => controller.abort();
    if (signal?.aborted) cancel();
    signal?.addEventListener("abort", cancel, {once: true});
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, delay);
    try {
      const response = await window.fetch(url, {...init, signal: controller.signal});
      // Keep the deadline active until the body has arrived, including images.
      const body = await response.arrayBuffer();
      return new Response([204, 205, 304].includes(response.status) ? null : body, {
        status: response.status, statusText: response.statusText, headers: response.headers,
      });
    } catch (error) {
      if (timedOut) {
        throw new Error("Время ожидания вышло. Проверьте статус задания или поста перед повтором: действие могло сохраниться.");
      }
      if (signal?.aborted) throw error;
      if (window.navigator?.onLine === false) {
        throw new Error("Нет подключения к интернету. Восстановите соединение и обновите данные.");
      }
      throw new Error("Не удалось связаться с приложением. Проверьте соединение и попробуйте обновить данные.");
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", cancel);
    }
  },
});
