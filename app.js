// 社内スライド検索 — minimal client helpers (tag editor + dirty tracking)

const X_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';

// A tag editor: chips backed by a hidden input whose value is a
// newline-separated tag list. Initialised from data-tags (JSON array).
function initTagEditor(root) {
  if (root.dataset.bound === "1") return;
  root.dataset.bound = "1";

  const box = root.querySelector("[data-tag-box]");
  const hidden = root.querySelector("[data-tag-hidden]");
  const input = root.querySelector("[data-tag-input]");
  const addBtn = root.querySelector("[data-tag-add]");

  let tags = [];
  try {
    tags = JSON.parse(root.dataset.tags || "[]");
  } catch (_) {
    tags = [];
  }

  function sync() {
    hidden.value = tags.join("\n");
    box.innerHTML = "";
    if (tags.length === 0) {
      const span = document.createElement("span");
      span.className = "tag-empty";
      span.textContent = "タグはまだありません";
      box.appendChild(span);
    }
    tags.forEach((t) => {
      const chip = document.createElement("span");
      chip.className = "tag-chip";
      chip.textContent = "#" + t;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.setAttribute("aria-label", t + " を削除");
      btn.innerHTML = X_SVG;
      btn.addEventListener("click", () => {
        tags = tags.filter((x) => x !== t);
        sync();
      });
      chip.appendChild(btn);
      box.appendChild(chip);
    });
  }

  function addTag(raw) {
    const t = (raw || "").trim().replace(/^#/, "");
    if (!t) return;
    if (tags.includes(t)) {
      input.value = "";
      return;
    }
    tags.push(t);
    input.value = "";
    sync();
  }

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      addTag(input.value);
    }
  });
  if (addBtn) addBtn.addEventListener("click", () => addTag(input.value));

  sync();
}

function initAll(scope) {
  (scope || document).querySelectorAll("[data-tag-editor]").forEach(initTagEditor);
}

document.addEventListener("DOMContentLoaded", () => initAll(document));
document.body && document.body.addEventListener("htmx:afterSwap", (e) => initAll(e.target));

// 対話検索: example chips fill the question input and focus it.
document.addEventListener("click", (e) => {
  const chip = e.target.closest("[data-chat-fill]");
  if (!chip) return;
  const input = document.querySelector(".chat-input");
  if (!input) return;
  input.value = chip.textContent.trim();
  input.focus();
});

// 対話検索: destructive action is deliberately confirmed even when navigating
// directly from a long-running conversation.
document.addEventListener("submit", (e) => {
  const form = e.target;
  if (!form.matches || !form.matches("[data-chat-delete]")) return;
  if (!window.confirm("この会話を削除しますか？\nこの操作は取り消せません。")) {
    e.preventDefault();
  }
});

// 対話検索: 詳細設定（定例シリーズ・検索対象）の折りたたみパネル。
// デフォルト（自動判定・全ソースON）以外の設定中はボタンに「変更あり」
// バッジを出し、今どんな条件で聞いているかを閉じたままでも示す。
(function initChatAdvanced() {
  const toggle = document.getElementById("chat-advanced-toggle");
  const panel = document.getElementById("chat-advanced");
  if (!toggle || !panel) return;

  // 選んだ設定は localStorage に保存し、次回訪問時に復元する。
  const STORAGE_KEY = "chatAdvancedSettings";

  function loadSaved() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      const data = JSON.parse(raw);
      return data && typeof data === "object" ? data : null;
    } catch (_) {
      return null;
    }
  }

  function saveSettings() {
    try {
      const data = {};
      const series = panel.querySelector('[name="seriesId"]');
      if (series) data.seriesId = series.value;
      const sources = panel.querySelectorAll('[name="source"]');
      if (sources.length) {
        data.sources = {};
        sources.forEach((cb) => {
          data.sources[cb.value] = cb.checked;
        });
      }
      localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
    } catch (_) {
      /* localStorage unavailable — persistence is best-effort */
    }
  }

  function restoreSettings() {
    const data = loadSaved();
    if (!data) return;
    const series = panel.querySelector('[name="seriesId"]');
    if (series && typeof data.seriesId === "string") {
      // 保存済みシリーズが削除されていたら自動判定（空値）に戻す。
      const exists = Array.from(series.options).some((o) => o.value === data.seriesId);
      series.value = exists ? data.seriesId : "";
    }
    if (data.sources && typeof data.sources === "object") {
      panel.querySelectorAll('[name="source"]').forEach((cb) => {
        if (typeof data.sources[cb.value] === "boolean") cb.checked = data.sources[cb.value];
      });
    }
  }

  toggle.addEventListener("click", () => {
    const open = panel.hasAttribute("hidden");
    if (open) panel.removeAttribute("hidden");
    else panel.setAttribute("hidden", "");
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  });

  const badge = toggle.querySelector(".chat-advanced-badge");
  function nonDefault() {
    const series = panel.querySelector('[name="seriesId"]');
    if (series && series.value) return true;
    const sources = panel.querySelectorAll('[name="source"]');
    for (const cb of sources) {
      if (!cb.checked) return true;
    }
    return false;
  }
  function syncBadge() {
    if (!badge) return;
    if (nonDefault()) badge.removeAttribute("hidden");
    else badge.setAttribute("hidden", "");
  }
  panel.addEventListener("change", () => {
    saveSettings();
    syncBadge();
  });
  restoreSettings();
  syncBadge();
})();

// Search controls are deliberately outside the HTMX swap target. Keep this
// small controller delegated so it also behaves correctly after navigation or
// partial page enhancement, while preserving keyboard focus on the toggle.
(function initSearchOptions() {
  let pendingFocus = null;

  function setFacetState(button) {
    const state = document.getElementById("facet-state");
    if (!state) return;
    const field = button.dataset.facetField;
    const value = button.dataset.facetValue;
    let hidden = Array.from(state.querySelectorAll("input")).find(
      (input) => input.name === field
    );
    const wasActive = button.getAttribute("aria-pressed") === "true";

    document.querySelectorAll("[data-facet-field]").forEach((candidate) => {
      if (candidate.dataset.facetField !== field) return;
      candidate.classList.remove("active");
      candidate.setAttribute("aria-pressed", "false");
    });

    if (wasActive) {
      if (hidden) hidden.remove();
    } else {
      if (!hidden) {
        hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = field;
        state.appendChild(hidden);
      }
      hidden.value = value;
      button.classList.add("active");
      button.setAttribute("aria-pressed", "true");
    }
    pendingFocus = { field, value };
  }

  function clearSearchState() {
    const query = document.querySelector('input[name="q"]');
    if (query) query.value = "";
    const state = document.getElementById("facet-state");
    if (state) state.replaceChildren();
    document.querySelectorAll("[data-facet-field]").forEach((button) => {
      button.classList.remove("active");
      button.setAttribute("aria-pressed", "false");
    });
    pendingFocus = { query: true };
  }

  function bind(root) {
    const toggle = (root || document).querySelector("#search-options-toggle");
    const panel = (root || document).querySelector("#search-options-panel");
    if (!toggle || !panel || toggle.dataset.bound === "1") return;
    toggle.dataset.bound = "1";
    const setOpen = (open) => {
      panel.toggleAttribute("hidden", !open);
      toggle.setAttribute("aria-expanded", String(open));
    };
    toggle.addEventListener("click", () => {
      const open = toggle.getAttribute("aria-expanded") !== "true";
      setOpen(open);
    });
    setOpen(toggle.getAttribute("aria-expanded") === "true");
  }
  document.addEventListener("DOMContentLoaded", () => bind(document));

  // Update the canonical hidden facet state in the capture phase, before htmx
  // serializes the request. Together with hx-sync=:replace this makes a rapid
  // second click carry both of the user's latest facet choices.
  document.addEventListener("click", (event) => {
    const facet = event.target.closest && event.target.closest("[data-facet-field]");
    if (facet) {
      setFacetState(facet);
      return;
    }
    const clear = event.target.closest && event.target.closest("[data-search-clear]");
    if (clear) clearSearchState();
  }, true);

  // An empty source list means "all sources" on the server. Prevent the
  // visible controls from reaching a misleading all-unchecked state before
  // htmx serializes the change event.
  document.addEventListener("change", (event) => {
    const input = event.target;
    if (!input.matches || !input.matches('#search-options-panel input[name="source"]')) return;
    const selected = document.querySelectorAll(
      '#search-options-panel input[name="source"]:checked'
    );
    if (selected.length === 0) input.checked = true;
  }, true);

  document.body && document.body.addEventListener("htmx:beforeRequest", (event) => {
    const requestElement = (
      event.detail
      && event.detail.requestConfig
      && event.detail.requestConfig.elt
    ) || event.target;
    if (!requestElement.closest || !requestElement.closest("#search-panel")) return;
    if (!requestElement.matches("[data-facet-field], [data-search-clear]")) {
      pendingFocus = null;
    }
  });

  document.body && document.body.addEventListener("htmx:afterSwap", (event) => {
    if (!pendingFocus || !event.target || event.target.id !== "facet-content") return;
    if (pendingFocus.query) {
      const query = document.querySelector('input[name="q"]');
      if (query) query.focus();
    } else {
      const replacement = Array.from(
        event.target.querySelectorAll("[data-facet-field]")
      ).find((button) => (
        button.dataset.facetField === pendingFocus.field
        && button.dataset.facetValue === pendingFocus.value
      ));
      if (replacement) replacement.focus();
      else {
        const toggle = document.getElementById("search-options-toggle");
        if (toggle) toggle.focus();
      }
    }
    pendingFocus = null;
  });
})();

// 対話検索: scroll newly appended turns into view.
document.body && document.body.addEventListener("htmx:afterSwap", (e) => {
  if (e.target && e.target.id === "chat-thread") {
    const last = e.target.lastElementChild;
    if (last) last.scrollIntoView({ behavior: "smooth", block: "start" });
  }
});

// 対話検索: optimistic turn — as soon as the user sends, show their message
// and a "検索中…" placeholder on the AI side, then replace that placeholder
// with the server-rendered turn once the answer comes back.
//
// htmx fires `htmx:beforeRequest` on the requesting element (the form) but
// `htmx:beforeSwap` on the swap *target* (#chat-thread), so we identify our
// request via `detail.requestConfig.elt` rather than the bubbled event target.
(function initChatOptimistic() {
  if (!document.body) return;
  let pending = null;
  let pendingQuestion = "";

  function fromChatForm(detail) {
    const elt = detail && detail.requestConfig && detail.requestConfig.elt;
    if (!elt || !elt.closest) return true; // can't tell → rely on `pending`
    return !!elt.closest("#chat-form");
  }

  function dropPending(restore) {
    if (!pending) return;
    pending.remove();
    if (restore) {
      const input = document.querySelector('#chat-form [name="question"]');
      if (input && !input.value) input.value = pendingQuestion;
    }
    pending = null;
    pendingQuestion = "";
  }

  document.body.addEventListener("htmx:beforeRequest", (e) => {
    const form = e.target;
    if (!form.matches || !form.matches("#chat-form")) return;
    const input = form.querySelector('[name="question"]');
    const question = ((input && input.value) || "").trim();
    if (!question) return;
    const thread = document.getElementById("chat-thread");
    const tpl = document.getElementById("chat-pending-template");
    if (!thread || !tpl) return;
    dropPending(false); // guard against any stale placeholder
    pendingQuestion = question;
    pending = tpl.content.firstElementChild.cloneNode(true);
    const slot = pending.querySelector("[data-pending-question]");
    if (slot) slot.textContent = question;
    thread.appendChild(pending);
    if (input) input.value = "";
    pending.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  document.body.addEventListener("htmx:beforeSwap", (e) => {
    if (!pending || !fromChatForm(e.detail)) return;
    const xhr = e.detail.xhr;
    if (xhr && xhr.status === 200) {
      // Replace the placeholder turn in place with the real answer. htmx 2.x
      // reads the per-swap style override from `swapOverride` (NOT `swapStyle`),
      // so without this the swap falls back to the form's `hx-swap="beforeend"`
      // and the answer gets appended *inside* the pending turn — leaving the
      // duplicate question + spinner. outerHTML replaces the pending node.
      e.detail.target = pending;
      e.detail.swapOverride = "outerHTML";
      e.detail.shouldSwap = true;
      pending = null; // consumed by the swap; afterRequest must not clean up
      pendingQuestion = "";
    }
    // Non-200: leave the placeholder for afterRequest/error cleanup below.
  });

  // Fail-safe: any completion that did NOT consume the placeholder (error
  // status, network failure) removes it and restores the question text.
  document.body.addEventListener("htmx:afterRequest", (e) => {
    if (fromChatForm(e.detail)) dropPending(true);
  });
  document.body.addEventListener("htmx:sendError", () => dropPending(true));
})();

// Toast popups. The server raises these via an HX-Trigger response header
// (`{"showToast": {"message": "...", "type": "success"|"error"}}`), e.g. the
// share-link add summary "新規 N 件追加、既存 N 件".
(function initToasts() {
  if (!document.body) return;

  function showToast(message, type) {
    if (!message) return;
    const wrap = document.getElementById("toast-container");
    if (!wrap) return;
    const el = document.createElement("div");
    el.className = "toast toast-" + (type === "error" ? "error" : "success");
    el.setAttribute("role", type === "error" ? "alert" : "status");

    const msg = document.createElement("div");
    msg.className = "toast-msg";
    msg.textContent = message;

    const close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.setAttribute("aria-label", "閉じる");
    close.textContent = "×";

    let removed = false;
    const remove = () => {
      if (removed) return;
      removed = true;
      el.classList.remove("show");
      setTimeout(() => el.remove(), 250);
    };
    close.addEventListener("click", remove);

    el.appendChild(msg);
    el.appendChild(close);
    wrap.appendChild(el);
    requestAnimationFrame(() => el.classList.add("show"));
    setTimeout(remove, 6000);
  }

  document.body.addEventListener("showToast", (e) => {
    const d = e.detail || {};
    showToast(d.message || d.value || "", d.type);
  });
})();
