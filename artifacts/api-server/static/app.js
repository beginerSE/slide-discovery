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
