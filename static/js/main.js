// ── API helper (CSRF + JSON) ─────────────────────────────────────────────────
const CSRF_TOKEN = document.querySelector('meta[name="csrf-token"]')?.content || "";

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
      if (statusEl) {
        statusEl.textContent = data.waiting
          ? `Очікуємо транскрипцію від Zoom (перевірка о ${data.not_before.slice(11, 16)})`
          : (labels[data.status] || data.status);
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

  // ── Score bars ─────────────────────────────────────────────────────────────
  document.querySelectorAll(".score-bar-fill").forEach((el) => {
    const pct = Math.max(0, Math.min(100, parseInt(el.dataset.score || "0", 10) || 0));
    el.style.width = pct + "%";
    el.style.background = pct >= 75 ? "#27ae60" : pct >= 50 ? "#f39c12" : "#e74c3c";
  });
});
