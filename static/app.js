// 提案スライド検索 — minimal client helpers (tag editor + dirty tracking)

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
      // Replace the placeholder turn in place with the real answer.
      e.detail.target = pending;
      e.detail.swapStyle = "outerHTML";
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
