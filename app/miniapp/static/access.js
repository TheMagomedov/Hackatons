"use strict";

// UX protection only. Every API request is authorized independently by the server.
window.TirazhAccess = (() => {
  const readActions = new Set([
    "close", "theme", "refresh", "open-post", "more", "filter", "clear-search",
    "content-layout", "calendar-day", "calendar-prev", "calendar-next", "calendar-today",
    "slide-prev", "slide-next", "slide-to", "show-workflow", "retry-image", "download-image",
    "visual-gallery", "open-visual", "business-archive", "job-visual",
  ]);
  const reason = "Доступ для просмотра: изменения, обращения к AI и публикации доступны владельцу и операторам.";
  const previous = new WeakMap();

  function resolve(access, preview = false) {
    if (preview && access == null) return {role: "owner", can_write: true};
    const role = ["owner", "operator", "reviewer"].includes(access?.role) ? access.role : "unknown";
    return {role, can_write: ["owner", "operator"].includes(role) && access?.can_write === true};
  }
  function requiresWrite(action) {
    // New actions are protected until explicitly classified as read-only.
    return typeof action === "string" && !readActions.has(action);
  }
  function assertWrite(access) {
    if (!resolve(access).can_write) throw new Error(reason);
  }
  function assertRequest(access, method = "GET") {
    if (!["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase())) assertWrite(access);
  }
  function protect(node) {
    if (!previous.has(node)) previous.set(node, {hidden: node.hidden, disabled: node.disabled});
    node.hidden = true;
    if ("disabled" in node) node.disabled = true;
    // A hidden form must not be submittable through keyboard or scripted focus.
    for (const field of node.querySelectorAll("input, textarea, select, button")) {
      if (!previous.has(field)) previous.set(field, {hidden: field.hidden, disabled: field.disabled});
      field.disabled = true;
    }
  }
  function apply(container, access) {
    const writable = resolve(access).can_write;
    const nodes = container.querySelectorAll("[data-action], [data-write], [data-chat], [data-form]");
    if (writable) {
      for (const node of container.querySelectorAll("[data-write], [data-write] input, [data-write] textarea, [data-write] select, [data-write] button")) {
        const saved = previous.get(node);
        if (!saved) continue;
        node.hidden = saved.hidden;
        if ("disabled" in node) node.disabled = saved.disabled;
        previous.delete(node);
      }
      return;
    }
    for (const node of nodes) {
      if (node.hasAttribute("data-write") || node.hasAttribute("data-chat")
        || node.hasAttribute("data-form") || requiresWrite(node.dataset.action)) {
        node.setAttribute("data-write", "");
        protect(node);
      }
    }
  }
  return Object.freeze({resolve, requiresWrite, assertWrite, assertRequest, apply, reason});
})();
