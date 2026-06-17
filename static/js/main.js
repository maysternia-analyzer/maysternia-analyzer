document.addEventListener("DOMContentLoaded", () => {

  // ── Drop zone ──────────────────────────────────────────────────────────────
  const dropZone = document.getElementById("drop-zone");
  const fileInput = document.getElementById("file-input");
  const fileNameEl = document.getElementById("file-name");

  if (dropZone && fileInput) {
    dropZone.addEventListener("click", () => fileInput.click());
    dropZone.addEventListener("dragover", (e) => { e.preventDefault(); dropZone.classList.add("dragover"); });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
    dropZone.addEventListener("drop", (e) => {
      e.preventDefault(); dropZone.classList.remove("dragover");
      const file = e.dataTransfer.files[0];
      if (file) setFile(file);
    });
    fileInput.addEventListener("change", () => { if (fileInput.files[0]) setFile(fileInput.files[0]); });
    function setFile(file) {
      const dt = new DataTransfer(); dt.items.add(file); fileInput.files = dt.files;
      if (fileNameEl) { fileNameEl.textContent = file.name; fileNameEl.style.display = "block"; }
    }
  }

  // ── Radio option highlight ─────────────────────────────────────────────────
  document.querySelectorAll(".radio-option").forEach((opt) => {
    const radio = opt.querySelector("input[type='radio']");
    if (radio) {
      const update = () => {
        document.querySelectorAll(".radio-option").forEach((o) => o.classList.remove("selected"));
        if (radio.checked) opt.classList.add("selected");
      };
      radio.addEventListener("change", update);
      if (radio.checked) opt.classList.add("selected");
    }
  });

  // ── Auto-refresh processing + browser notification ─────────────────────────
  const processingCard = document.getElementById("processing-card");
  if (processingCard) {
    const recordId = processingCard.dataset.recordId;
    if (Notification.permission === "default") Notification.requestPermission();
    const poll = setInterval(async () => {
      const res = await fetch(`/record/${recordId}/status`);
      const data = await res.json();
      if (data.status === "done" || data.status === "error") {
        clearInterval(poll);
        if (Notification.permission === "granted") {
          new Notification("Майстерня Аналізатор", {
            body: data.status === "done" ? "✅ Аналіз завершено — запис готовий!" : "❌ Помилка при обробці запису",
            icon: "/static/favicon.ico",
          });
        }
        location.reload();
      }
      const statusEl = document.getElementById("processing-status");
      if (statusEl) {
        if (data.status === "processing") statusEl.textContent = "Транскрибуємо запис...";
        if (data.status === "analyzing") statusEl.textContent = "AI аналізує розмову...";
      }
    }, 3000);
  }

  // ── Comment save ───────────────────────────────────────────────────────────
  const commentBtn = document.getElementById("save-comment-btn");
  if (commentBtn) {
    commentBtn.addEventListener("click", async () => {
      const text = document.getElementById("comment-field").value;
      const recordId = commentBtn.dataset.recordId;
      await fetch(`/record/${recordId}/comment`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ comment: text }),
      });
      commentBtn.textContent = "Збережено ✓";
      commentBtn.classList.add("btn-outline"); commentBtn.classList.remove("btn-primary");
      setTimeout(() => {
        commentBtn.textContent = "Зберегти коментар";
        commentBtn.classList.remove("btn-outline"); commentBtn.classList.add("btn-primary");
      }, 2500);
    });
  }

  // ── Re-analyze ─────────────────────────────────────────────────────────────
  const reanalyzeBtn = document.getElementById("reanalyze-btn");
  if (reanalyzeBtn) {
    reanalyzeBtn.addEventListener("click", async () => {
      const recordId = reanalyzeBtn.dataset.recordId;
      reanalyzeBtn.textContent = "Аналізуємо..."; reanalyzeBtn.disabled = true;
      await fetch(`/record/${recordId}/reanalyze`, { method: "POST" });
      const wait = setInterval(async () => {
        const res = await fetch(`/record/${recordId}/status`);
        const data = await res.json();
        if (data.status === "done" || data.status === "error") { clearInterval(wait); location.reload(); }
      }, 3000);
    });
  }

  // ── Sale result buttons ────────────────────────────────────────────────────
  document.querySelectorAll(".sale-result-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const recordId = btn.dataset.recordId;
      const val = parseInt(btn.dataset.val);
      await fetch(`/record/${recordId}/sale_result`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sale_made: val === 1, sale_amount: null }),
      });
      location.reload();
    });
  });

  const clearSaleBtn = document.getElementById("clear-sale-btn");
  if (clearSaleBtn) {
    clearSaleBtn.addEventListener("click", async () => {
      const recordId = clearSaleBtn.dataset.recordId;
      await fetch(`/record/${recordId}/sale_result`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sale_made: null, sale_amount: null }),
      });
      location.reload();
    });
  }

  const saveAmountBtn = document.getElementById("save-amount-btn");
  if (saveAmountBtn) {
    saveAmountBtn.addEventListener("click", async () => {
      const recordId = saveAmountBtn.dataset.recordId;
      const amount = parseFloat(document.getElementById("sale-amount-input").value) || null;
      await fetch(`/record/${recordId}/sale_result`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sale_made: true, sale_amount: amount }),
      });
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
      const recordId = editForm.dataset.recordId;
      const type = editForm.querySelector("[name=record_type]").value;
      const name = editForm.querySelector("[name=person_name]").value;
      await fetch(`/record/${recordId}/meta`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ record_type: type, person_name: name }),
      });
      location.reload();
    });
  }

  // ── Table row click ────────────────────────────────────────────────────────
  document.querySelectorAll("tr.clickable").forEach((row) => {
    row.addEventListener("click", () => { if (row.dataset.href) window.location.href = row.dataset.href; });
  });

  // ── Table sort ─────────────────────────────────────────────────────────────
  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.style.cursor = "pointer";
    th.title = "Сортувати";
    th.addEventListener("click", () => {
      const table = th.closest("table");
      const tbody = table.querySelector("tbody");
      const col = parseInt(th.dataset.sort);
      const asc = th.dataset.dir !== "asc";
      th.dataset.dir = asc ? "asc" : "desc";

      // Reset other headers
      table.querySelectorAll("th[data-sort]").forEach((h) => {
        h.querySelector(".sort-arrow")?.remove();
      });
      const arrow = document.createElement("span");
      arrow.className = "sort-arrow";
      arrow.textContent = asc ? " ▲" : " ▼";
      arrow.style.color = "var(--accent)";
      th.appendChild(arrow);

      const rows = Array.from(tbody.querySelectorAll("tr"));
      rows.sort((a, b) => {
        const av = a.cells[col]?.dataset.val ?? a.cells[col]?.textContent.trim() ?? "";
        const bv = b.cells[col]?.dataset.val ?? b.cells[col]?.textContent.trim() ?? "";
        const an = parseFloat(av), bn = parseFloat(bv);
        if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
        return asc ? av.localeCompare(bv, "uk") : bv.localeCompare(av, "uk");
      });
      rows.forEach((r) => tbody.appendChild(r));
    });
  });

});

// ── Score bar color ────────────────────────────────────────────────────────
function scoreColor(s) {
  return s >= 75 ? "#27ae60" : s >= 50 ? "#f39c12" : "#e74c3c";
}
document.querySelectorAll(".score-bar-fill").forEach((el) => {
  const pct = parseInt(el.dataset.score || 0);
  el.style.width = pct + "%";
  el.style.background = scoreColor(pct);
});
