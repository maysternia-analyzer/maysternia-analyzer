// ── API helper (CSRF + JSON) ─────────────────────────────────────────────────
const CSRF_TOKEN = document.querySelector('meta[name="csrf-token"]')?.content || "";

// ── Помилки JavaScript → журнал на сервері (не більше 5 зі сторінки) ─────────
(() => {
  if (!CSRF_TOKEN || !document.querySelector(".sidebar")) return;   // лише для авторизованих сторінок
  let sent = 0;
  const report = (message, source, line, column, stack) => {
    if (sent >= 5) return;
    sent += 1;
    try {
      fetch("/api/client-error", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": CSRF_TOKEN },
        credentials: "same-origin",
        keepalive: true,
        body: JSON.stringify({ message: String(message || "").slice(0, 500), source, line, column,
                               stack: String(stack || "").slice(0, 4000), url: location.href }),
      }).catch(() => {});
    } catch (_) { /* журнал не повинен ламати сторінку */ }
  };
  window.addEventListener("error", (e) => {
    if (!e.message) return;   // помилка завантаження картинки/скрипта — без тексту
    report(e.message, e.filename, e.lineno, e.colno, e.error && e.error.stack);
  });
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    report("Promise: " + (r && r.message ? r.message : String(r)), "", 0, 0, r && r.stack);
  });
})();

async function apiPost(url, data = {}) {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": CSRF_TOKEN },
      body: JSON.stringify(data),
      credentials: "same-origin",
    });
    const isJson = (res.headers.get("content-type") || "").includes("application/json");
    if (res.redirected || !isJson) {
      // сесія завершилась: сервер перенаправив на сторінку входу
      return { ok: false, error: "Сесія завершилась — оновіть сторінку та увійдіть знову" };
    }
    const body = await res.json();
    if (!res.ok || body.ok === false) {
      return { ok: false, error: body.error || `Помилка сервера (${res.status})` };
    }
    return { ok: true, ...body };
  } catch (e) {
    return { ok: false, error: "Немає зʼєднання з сервером" };
  }
}
window.apiPost = apiPost;

document.addEventListener("DOMContentLoaded", () => {

  // ── Мобільне меню ──────────────────────────────────────────────────────────
  const menuToggle = document.getElementById("menu-toggle");
  if (menuToggle) {
    menuToggle.addEventListener("click", () => {
      const open = document.body.classList.toggle("menu-open");
      menuToggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  // ── Drop zone ──────────────────────────────────────────────────────────────
  const dropZone = document.getElementById("drop-zone");
  const fileInput = document.getElementById("file-input");
  const fileNameEl = document.getElementById("file-name");

  if (dropZone && fileInput) {
    const showName = (file) => {
      if (fileNameEl) { fileNameEl.textContent = file.name; fileNameEl.style.display = "block"; }
    };
    dropZone.addEventListener("click", () => fileInput.click());
    dropZone.addEventListener("dragover", (e) => { e.preventDefault(); dropZone.classList.add("dragover"); });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
    dropZone.addEventListener("drop", (e) => {
      e.preventDefault(); dropZone.classList.remove("dragover");
      const file = e.dataTransfer.files[0];
      if (!file) return;
      const dt = new DataTransfer(); dt.items.add(file); fileInput.files = dt.files;
      showName(file);
    });
    fileInput.addEventListener("change", () => { if (fileInput.files[0]) showName(fileInput.files[0]); });
  }

  // ── Radio option highlight ─────────────────────────────────────────────────
  const radioOptions = document.querySelectorAll(".radio-option");
  radioOptions.forEach((opt) => {
    const radio = opt.querySelector("input[type='radio']");
    if (!radio) return;
    radio.addEventListener("change", () => {
      radioOptions.forEach((o) => o.classList.remove("selected"));
      if (radio.checked) opt.classList.add("selected");
    });
    if (radio.checked) opt.classList.add("selected");
  });

  // ── Auto-refresh processing + browser notification ─────────────────────────
  const processingCard = document.getElementById("processing-card");
  if (processingCard) {
    const recordId = processingCard.dataset.recordId;
    const canNotify = "Notification" in window;
    if (canNotify && Notification.permission === "default") {
      Notification.requestPermission().catch(() => {});
    }
    const statusEl = document.getElementById("processing-status");
    const noteEl = document.getElementById("processing-note");
    const labels = {
      queued: "В черзі на обробку...",
      processing: "Отримуємо транскрипцію запису...",
      analyzing: "AI аналізує розмову...",
    };
    const labelFor = (data) => (data.analysis_only && data.status === "processing")
      ? labels.analyzing : (labels[data.status] || data.status);
    const poll = setInterval(async () => {
      let data;
      try {
        const res = await fetch(`/record/${recordId}/status`, { credentials: "same-origin" });
        if (res.status === 401 || res.status === 404) { clearInterval(poll); return; }
        if (!res.ok) return;
        data = await res.json();
      } catch (_) { return; }
      if (data.status === "done" || data.status === "error") {
        clearInterval(poll);
        if (canNotify && Notification.permission === "granted") {
          try {
            new Notification("Майстерня Аналізатор", {
              body: data.status === "done" ? "✅ Аналіз завершено — запис готовий!" : "❌ Помилка при обробці запису",
            });
          } catch (_) { /* мобільні браузери можуть не підтримувати */ }
        }
        location.reload();
        return;
      }
      if (statusEl && !processingCard.classList.contains("alert")) {
        statusEl.textContent = data.waiting
          ? `Очікуємо транскрипцію від Zoom (перевірка о ${data.not_before.slice(11, 16)})`
          : labelFor(data);
      }
      if (noteEl && data.error_message) noteEl.textContent = data.error_message;
    }, 4000);
  }

  // ── Comment save ───────────────────────────────────────────────────────────
  const commentBtn = document.getElementById("save-comment-btn");
  if (commentBtn) {
    commentBtn.addEventListener("click", async () => {
      const text = document.getElementById("comment-field").value;
      commentBtn.disabled = true;
      const result = await apiPost(`/record/${commentBtn.dataset.recordId}/comment`, { comment: text });
      commentBtn.disabled = false;
      if (!result.ok) { alert(result.error); return; }
      commentBtn.textContent = "Збережено ✓";
      commentBtn.classList.add("btn-outline"); commentBtn.classList.remove("btn-primary");
      setTimeout(() => {
        commentBtn.textContent = "Зберегти коментар";
        commentBtn.classList.remove("btn-outline"); commentBtn.classList.add("btn-primary");
      }, 2500);
    });
  }

  // ── Re-analyze / retry ─────────────────────────────────────────────────────
  document.querySelectorAll(".reanalyze-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const original = btn.textContent;
      btn.textContent = "Запускаємо..."; btn.disabled = true;
      const result = await apiPost(`/record/${btn.dataset.recordId}/reanalyze`, { mode: btn.dataset.mode || "auto" });
      if (!result.ok) {
        alert(result.error);
        btn.textContent = original; btn.disabled = false;
        return;
      }
      location.reload();
    });
  });

  // ── Sale result ────────────────────────────────────────────────────────────
  document.querySelectorAll(".sale-result-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const result = await apiPost(`/record/${btn.dataset.recordId}/sale_result`,
                                   { sale_made: btn.dataset.val === "1" });
      if (!result.ok) { alert(result.error); return; }
      location.reload();
    });
  });

  const clearSaleBtn = document.getElementById("clear-sale-btn");
  if (clearSaleBtn) {
    clearSaleBtn.addEventListener("click", async () => {
      const result = await apiPost(`/record/${clearSaleBtn.dataset.recordId}/sale_result`,
                                   { sale_made: null, sale_amount: null });
      if (!result.ok) { alert(result.error); return; }
      location.reload();
    });
  }

  const saveAmountBtn = document.getElementById("save-amount-btn");
  document.getElementById("sale-amount-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); saveAmountBtn?.click(); }
  });
  if (saveAmountBtn) {
    saveAmountBtn.addEventListener("click", async () => {
      const raw = document.getElementById("sale-amount-input").value.trim();
      const amount = raw === "" ? null : Number(raw);
      if (amount !== null && (!Number.isFinite(amount) || amount < 0)) { alert("Введіть коректну суму"); return; }
      const result = await apiPost(`/record/${saveAmountBtn.dataset.recordId}/sale_result`,
                                   { sale_made: true, sale_amount: amount });
      if (!result.ok) { alert(result.error); return; }
      saveAmountBtn.textContent = "Збережено ✓";
      setTimeout(() => { saveAmountBtn.textContent = "Зберегти"; }, 2000);
    });
  }

  // ── Inline edit (type + name on record page) ───────────────────────────────
  const editBtn = document.getElementById("edit-meta-btn");
  const editForm = document.getElementById("edit-meta-form");
  if (editBtn && editForm) {
    editBtn.addEventListener("click", () => {
      editForm.style.display = editForm.style.display === "none" ? "flex" : "none";
    });
    editForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const type = editForm.querySelector("[name=record_type]").value;
      const name = editForm.querySelector("[name=person_name]").value.trim();
      if (type !== editForm.dataset.currentType &&
          !confirm("Змінити тип запису? AI-аналіз буде виконано заново.")) return;
      const result = await apiPost(`/record/${editForm.dataset.recordId}/meta`,
                                   { record_type: type, person_name: name });
      if (!result.ok) { alert(result.error); return; }
      location.reload();
    });
  }

  // ── Admin: password form toggle + confirm forms ────────────────────────────
  document.querySelectorAll(".pw-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const form = document.getElementById(`pw-form-${btn.dataset.userId}`);
      if (form) form.style.display = form.style.display === "none" ? "flex" : "none";
    });
  });
  document.querySelectorAll("form.confirm-form").forEach((form) => {
    form.addEventListener("submit", (e) => {
      if (!confirm(form.dataset.confirm || "Підтвердити дію?")) e.preventDefault();
    });
  });

  // ── Table row click ────────────────────────────────────────────────────────
  document.querySelectorAll("tr.clickable").forEach((row) => {
    row.addEventListener("click", (e) => {
      if (e.target.closest("a, button, input, select, textarea")) return;
      if (row.dataset.href) window.location.href = row.dataset.href;
    });
  });

  // ── Table sort ─────────────────────────────────────────────────────────────
  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.style.cursor = "pointer";
    th.title = "Сортувати";
    th.addEventListener("click", () => {
      const table = th.closest("table");
      const tbody = table.querySelector("tbody");
      const col = parseInt(th.dataset.sort, 10);
      const asc = th.dataset.dir !== "asc";
      th.dataset.dir = asc ? "asc" : "desc";

      table.querySelectorAll("th[data-sort] .sort-arrow").forEach((a) => a.remove());
      const arrow = document.createElement("span");
      arrow.className = "sort-arrow";
      arrow.textContent = asc ? " ▲" : " ▼";
      arrow.style.color = "var(--accent)";
      th.appendChild(arrow);

      const value = (row) => {
        const cell = row.cells[col];
        if (!cell) return "";
        return cell.dataset.val ?? cell.textContent.trim();
      };
      const rows = Array.from(tbody.querySelectorAll("tr"));
      rows.sort((a, b) => {
        const av = value(a), bv = value(b);
        const an = Number(av), bn = Number(bv);
        if (av !== "" && bv !== "" && !isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
        return asc ? av.localeCompare(bv, "uk") : bv.localeCompare(av, "uk");
      });
      rows.forEach((r) => tbody.appendChild(r));
    });
  });

  // ── Timecodes: перемотка плеєра / прокрутка транскрипції ────────────────────
  const toSeconds = (label) => {
    const parts = String(label).split(":").map(Number);
    if (parts.some(isNaN)) return null;
    return parts.reduce((acc, value) => acc * 60 + value, 0);
  };
  document.addEventListener("click", (e) => {
    const button = e.target.closest(".tc");
    if (!button) return;
    const seconds = toSeconds(button.dataset.t);
    if (seconds === null) return;
    const player = document.getElementById("media-player");
    if (player) {
      player.currentTime = seconds;
      player.play().catch(() => {});
      player.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    const lines = Array.from(document.querySelectorAll("#transcript .t-line[data-t]"));
    let target = null;
    for (const line of lines) {
      if (Number(line.dataset.t) <= seconds) target = line; else break;
    }
    if (target) {
      document.querySelectorAll("#transcript .t-line.highlight").forEach((l) => l.classList.remove("highlight"));
      target.classList.add("highlight");
      const box = document.getElementById("transcript");
      box.scrollTop = target.offsetTop - box.offsetTop - box.clientHeight / 2;
      if (!box.contains(button)) showBackButton(window.scrollY);
      box.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  });

  // Після переходу до транскрипції — кнопка повернення на попереднє місце сторінки
  function showBackButton(scrollY) {
    let back = document.getElementById("back-to-place");
    if (!back) {
      back = document.createElement("button");
      back.id = "back-to-place";
      back.type = "button";
      back.className = "btn btn-primary btn-sm back-to-place";
      back.textContent = "↩ Назад до аналізу";
      document.body.appendChild(back);
    }
    back.onclick = () => { window.scrollTo({ top: scrollY, behavior: "smooth" }); back.remove(); };
  }

  // ── Копіювання тексту ──────────────────────────────────────────────────────
  document.querySelectorAll(".copy-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const source = document.getElementById(btn.dataset.copyTarget);
      if (!source) return;
      try {
        await navigator.clipboard.writeText(source.innerText.trim());
        btn.textContent = "✓ Скопійовано";
      } catch (_) {
        btn.textContent = "Виділіть текст вручну";
      }
      setTimeout(() => { btn.textContent = "📋 Копіювати"; }, 2000);
    });
  });

  // ── Автовідправка форм (зміна ролі) ────────────────────────────────────────
  document.querySelectorAll("select.auto-submit").forEach((select) => {
    select.addEventListener("change", () => select.form && select.form.submit());
  });

  // ── Редактор чек-листа ─────────────────────────────────────────────────────
  const criteriaList = document.getElementById("criteria-list");
  if (criteriaList) {
    const template = document.getElementById("criterion-template");
    const form = document.getElementById("checklist-form");
    let dirty = false;
    form.addEventListener("input", () => { dirty = true; });
    form.addEventListener("submit", () => { dirty = false; });
    window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
    // Enter у полі назви не повинен одразу зберігати весь чек-лист
    form.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.target.tagName === "INPUT") e.preventDefault();
    });
    document.getElementById("add-criterion")?.addEventListener("click", () => {
      if (criteriaList.querySelectorAll(".criterion-row").length >= 20) {
        alert("Максимум 20 критеріїв");
        return;
      }
      const row = template.content.firstElementChild.cloneNode(true);
      criteriaList.appendChild(row);
      dirty = true;
      row.querySelector("input[name=criterion_title]").focus();
    });
    criteriaList.addEventListener("click", (e) => {
      const row = e.target.closest(".criterion-row");
      if (!row) return;
      if (e.target.closest(".remove-criterion")) {
        if (criteriaList.querySelectorAll(".criterion-row").length <= 1) {
          alert("У чек-листі має залишитися хоча б один критерій");
          return;
        }
        row.remove();
        dirty = true;
      } else if (e.target.closest(".move-up") && row.previousElementSibling) {
        criteriaList.insertBefore(row, row.previousElementSibling);
        dirty = true;
      } else if (e.target.closest(".move-down") && row.nextElementSibling) {
        criteriaList.insertBefore(row.nextElementSibling, row);
        dirty = true;
      }
    });
  }

  // ── Масовий переаналіз: підтвердження з реальною кількістю записів ──────────
  const reanalyzeForm = document.getElementById("reanalyze-form");
  if (reanalyzeForm) {
    reanalyzeForm.addEventListener("submit", async (e) => {
      if (reanalyzeForm.dataset.confirmed) return;
      e.preventDefault();
      const params = new URLSearchParams(new FormData(reanalyzeForm));
      params.delete("csrf_token");
      let text = "Поставити записи за вибраний період у чергу на повторний аналіз?";
      try {
        const res = await fetch(`${reanalyzeForm.dataset.countUrl}?${params}`, { credentials: "same-origin" });
        const data = await res.json();
        if (data.ok && !data.count) { alert("За вибраний період немає записів з транскрипцією"); return; }
        if (data.ok) text = `Переаналізувати ${data.count} записів${data.total > data.count ? ` (з ${data.total}; максимум ${data.limit} за раз)` : ""}? `
             + "Кожен запис — окремий платний запит до Claude.";
      } catch (_) { /* покажемо загальне питання */ }
      if (confirm(text)) { reanalyzeForm.dataset.confirmed = "1"; reanalyzeForm.submit(); }
    });
  }

  // ── Налаштування: поле власної моделі лише для «Інша модель…» ───────────────
  const modelSelect = document.getElementById("anthropic_model");
  const customModel = document.getElementById("anthropic_model_custom");
  if (modelSelect && customModel) {
    const sync = () => { customModel.style.display = modelSelect.value === "custom" ? "" : "none"; };
    modelSelect.addEventListener("change", sync);
    sync();
  }

  // ── Score bars ─────────────────────────────────────────────────────────────
  document.querySelectorAll(".score-bar-fill").forEach((el) => {
    const pct = Math.max(0, Math.min(100, parseInt(el.dataset.score || "0", 10) || 0));
    el.style.width = pct + "%";
    el.style.background = pct >= 75 ? "#27ae60" : pct >= 50 ? "#f39c12" : "#e74c3c";
  });
});
