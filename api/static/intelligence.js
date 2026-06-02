(function () {
  const root = document.getElementById("intel-root");
  const projectId = root.dataset.projectId;
  const enc = encodeURIComponent(projectId);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function loadRecs() {
    try {
      const rows = await (await fetch(`/api/projects/${enc}/recommendations?limit=10`)).json();
      const host = document.getElementById("rec-list");
      if (!rows.length) {
        host.innerHTML = '<div class="muted">No recommendations yet. Click "Get AI suggestions" to ask the LLM.</div>';
        return;
      }
      host.innerHTML = rows.map(r => `
        <div class="panel" style="margin-bottom: 14px;">
          <div style="padding: 12px 16px;">
            <div><strong>${esc(r.title)}</strong>
              <span class="badge ${r.status === 'applied' ? 'ok' : 'ghost'}">${esc(r.status)}</span></div>
            <div class="muted" style="font-size: 12px;">${new Date(r.created_at).toLocaleString()}</div>
            <p style="margin: 10px 0; font-size: 13px;">${esc(r.rationale || "")}</p>
            <pre style="background: var(--panel-2); padding: 10px; border-radius: 4px; font-size: 12px;">${esc(JSON.stringify(r.changes, null, 2))}</pre>
            ${r.status === "pending" ? `<button class="btn small primary" data-apply="${r.rec_id}">Apply</button>` : ""}
          </div>
        </div>`).join("");
      host.querySelectorAll("[data-apply]").forEach(b => b.addEventListener("click", async () => {
        if (!await confirmModal("Apply these settings to the project?",
            { title: "Apply recommendation", okLabel: "Apply", danger: false })) return;
        const r = await fetch(`/api/projects/${enc}/recommendations/${b.dataset.apply}/apply`, { method: "POST" });
        const out = await r.json();
        if (out.error) toast.error("Failed: " + out.error);
        else toast.ok("Applied: " + Object.keys(out.applied || {}).join(", "));
        loadRecs();
      }));
    } catch (e) {
      document.getElementById("rec-list").innerHTML = `<div class="pl-neg">Error: ${esc(e.message)}</div>`;
    }
  }

  async function loadAnomalies() {
    try {
      const rows = await (await fetch(`/api/projects/${enc}/anomalies?limit=20`)).json();
      const tbody = document.querySelector("#anom-table tbody");
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="empty">None detected.</td></tr>';
        return;
      }
      tbody.innerHTML = rows.map(a => `
        <tr>
          <td>${new Date(a.detected_at).toLocaleString()}</td>
          <td class="${a.severity === 'error' ? 'pl-neg' : ''}">${esc(a.severity)}</td>
          <td><code>${esc(a.kind)}</code></td>
          <td>${a.baseline_value != null ? a.baseline_value.toFixed(2) : "—"}</td>
          <td>${a.observed_value != null ? a.observed_value : "—"}</td>
        </tr>`).join("");
    } catch (e) { console.error(e); }
  }

  async function loadTemplates() {
    try {
      const rows = await (await fetch("/api/strategy_templates")).json();
      const host = document.getElementById("tpl-list");
      host.innerHTML = rows.map(t => `
        <div class="card" style="margin-bottom: 12px;">
          <div><strong>${esc(t.name)}</strong></div>
          <div class="muted" style="font-size: 12px; margin: 6px 0;">${esc(t.description)}</div>
          <button class="btn small primary" data-tpl="${esc(t.id)}">Apply to this project</button>
        </div>`).join("");
      host.querySelectorAll("[data-tpl]").forEach(b => b.addEventListener("click", async () => {
        if (!await confirmModal("Apply this template's settings to this project?",
            { title: "Apply strategy template", okLabel: "Apply", danger: false })) return;
        const r = await fetch(`/api/projects/${enc}/strategy_templates/${b.dataset.tpl}/apply`, { method: "POST" });
        const out = await r.json();
        if (out.error) toast.error("Failed: " + out.error);
        else toast.ok(`Applied ${Object.keys(out.applied || {}).length} settings`);
      }));
    } catch (e) { console.error(e); }
  }

  document.getElementById("rec-build").addEventListener("click", async () => {
    const btn = document.getElementById("rec-build");
    btn.disabled = true; btn.textContent = "Thinking…";
    try {
      const r = await fetch(`/api/projects/${enc}/recommendations/build`, { method: "POST" });
      const out = await r.json();
      if (out.error) {
        const detail = out.raw ? ` (raw: ${out.raw.slice(0, 120)}…)` : "";
        toast.error("Failed: " + out.error + detail);
      } else {
        loadRecs();
        toast.ok("AI recommendation built");
      }
    } catch (e) {
      toast.error("Network error: " + e.message);
    } finally {
      btn.disabled = false; btn.textContent = "Get AI suggestions";
    }
  });
  document.getElementById("anom-detect").addEventListener("click", async () => {
    const r = await fetch(`/api/projects/${enc}/anomalies/detect`, { method: "POST" });
    const out = await r.json();
    toast.info(`Detected ${(out.anomalies || []).length} anomaly(ies)`);
    loadAnomalies();
  });

  loadRecs(); loadAnomalies(); loadTemplates();
  setInterval(loadAnomalies, 30000);
})();
