// Risk page — greeks + kill switches CRUD + earnings lookup.

(function () {
  const root = document.getElementById("risk-root");
  const projectId = root.dataset.projectId;
  const enc = encodeURIComponent(projectId);

  const dialog = document.getElementById("limit-dialog");
  const form = document.getElementById("limit-form");
  const formTitle = document.getElementById("limit-form-title");

  const escapeHtml = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtNum = v => (v == null ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 }));
  const fmtTime = iso => (iso ? new Date(iso).toLocaleString() : "—");

  async function loadGreeks() {
    try {
      const r = await fetch(`/api/projects/${enc}/risk/greeks`);
      const g = await r.json();
      document.getElementById("greek-delta").textContent = fmtNum(g.delta);
      document.getElementById("greek-theta").textContent = fmtNum(g.theta);
      document.getElementById("greek-vega").textContent  = fmtNum(g.vega);
      document.getElementById("greek-gamma").textContent = (g.gamma ?? 0).toFixed(4);
    } catch (e) {
      ["greek-delta","greek-theta","greek-vega","greek-gamma"].forEach(id =>
        document.getElementById(id).textContent = "err"
      );
    }
  }

  async function loadLimits() {
    try {
      const r = await fetch(`/api/projects/${enc}/risk/limits`);
      const rows = await r.json();
      const tbody = document.querySelector("#limits-table tbody");
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty">No kill switches configured yet.</td></tr>';
        renderBanner(null);
        return;
      }
      tbody.innerHTML = rows.map(l => `
        <tr>
          <td><code>${escapeHtml(l.limit_type)}</code></td>
          <td>${escapeHtml(l.threshold)}</td>
          <td>${l.window_minutes ?? "—"}</td>
          <td><span class="badge ${l.action === 'LIQUIDATE' ? 'danger' : 'warn'}">${escapeHtml(l.action)}</span></td>
          <td>${l.enabled ? "✓" : "—"}</td>
          <td>${l.breach_count}</td>
          <td>${fmtTime(l.last_breached_at)}${l.last_breach_value != null ? ` (${l.last_breach_value})` : ""}</td>
          <td>
            <button class="btn small ghost" data-edit='${JSON.stringify(l)}'>Edit</button>
            <button class="btn small danger" data-delete="${l.limit_id}">Delete</button>
          </td>
        </tr>`).join("");
      tbody.querySelectorAll("[data-edit]").forEach(b => b.addEventListener("click", () => {
        const lim = JSON.parse(b.dataset.edit);
        openDialog(lim);
      }));
      tbody.querySelectorAll("[data-delete]").forEach(b => b.addEventListener("click", async () => {
        if (!await confirmModal("Delete this kill switch?",
            { title: "Delete kill switch", okLabel: "Delete" })) return;
        await fetch(`/api/projects/${enc}/risk/limits/${b.dataset.delete}`, { method: "DELETE" });
        loadLimits();
        toast.ok("Kill switch deleted");
      }));

      // Banner: most recent breach
      const breached = rows.filter(l => l.last_breached_at);
      breached.sort((a,b) => b.last_breached_at.localeCompare(a.last_breached_at));
      renderBanner(breached[0] || null);
    } catch (e) {
      document.querySelector("#limits-table tbody").innerHTML =
        `<tr><td colspan="8" class="empty">Error: ${escapeHtml(e.message)}</td></tr>`;
    }
  }

  function renderBanner(breach) {
    const host = document.getElementById("breach-banner");
    if (!breach) { host.innerHTML = ""; return; }
    host.innerHTML = `
      <div class="alert error">
        <div class="alert-title">🛑 Kill switch breached: ${escapeHtml(breach.limit_type)}</div>
        <div class="alert-detail">
          Observed ${breach.last_breach_value} (threshold ${breach.threshold}) on
          ${fmtTime(breach.last_breached_at)}.
          Action taken: <strong>${escapeHtml(breach.action)}</strong>.
          Project has been set to inactive — re-enable from the dashboard once you've reviewed.
        </div>
      </div>`;
  }

  function openDialog(existing) {
    formTitle.textContent = existing ? "Edit Kill Switch" : "New Kill Switch";
    form.elements["limit_id"].value = existing?.limit_id ?? "";
    form.elements["limit_type"].value = existing?.limit_type ?? "daily_loss";
    form.elements["threshold"].value = existing?.threshold ?? "";
    form.elements["window_minutes"].value = existing?.window_minutes ?? "";
    form.elements["action"].value = existing?.action ?? "HALT";
    form.elements["enabled"].checked = existing ? existing.enabled : true;
    dialog.showModal();
  }
  dialog.querySelectorAll("[data-close]").forEach(b =>
    b.addEventListener("click", () => dialog.close())
  );
  document.getElementById("add-limit").addEventListener("click", () => openDialog(null));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const payload = {
      limit_type: fd.get("limit_type"),
      threshold: parseFloat(fd.get("threshold")),
      action: fd.get("action"),
      window_minutes: fd.get("window_minutes") ? parseInt(fd.get("window_minutes"), 10) : null,
      enabled: fd.get("enabled") === "on",
    };
    const lid = fd.get("limit_id");
    if (lid) payload.limit_id = parseInt(lid, 10);
    const resp = await fetch(`/api/projects/${enc}/risk/limits`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (resp.ok) { dialog.close(); loadLimits(); toast.ok("Risk limit saved"); }
    else { toast.error("Save failed"); }
  });

  document.getElementById("risk-refresh").addEventListener("click", () => {
    loadGreeks(); loadLimits();
  });

  document.getElementById("evaluate-now").addEventListener("click", async () => {
    const r = await fetch(`/api/projects/${enc}/risk/evaluate`, { method: "POST" });
    const out = await r.json();
    if ((out.breaches || []).length) {
      toast.error("Breach detected — reloading…");
      setTimeout(() => location.reload(), 800);
    } else {
      toast.ok("All kill switches clear");
    }
  });

  document.getElementById("earn-fetch").addEventListener("click", async () => {
    const ticker = document.getElementById("earn-ticker").value.trim().toUpperCase();
    if (!ticker) return;
    const out = document.getElementById("earn-result");
    out.textContent = "fetching…";
    try {
      const r = await fetch(`/api/projects/${enc}/risk/earnings/${ticker}`);
      const d = await r.json();
      if (d.next_earnings_date) {
        const dt = new Date(d.next_earnings_date);
        const days = Math.ceil((dt - new Date()) / 86400000);
        out.textContent = `${ticker}: next earnings ${d.next_earnings_date} (${days} days)`;
      } else {
        out.textContent = `${ticker}: no upcoming earnings found.`;
      }
    } catch (e) {
      out.textContent = "error: " + e.message;
    }
  });

  loadGreeks();
  loadLimits();
  setInterval(() => { loadGreeks(); loadLimits(); }, 15000);
})();
