(function () {
  const root = document.getElementById("rel-root");
  const projectId = root.dataset.projectId;
  const enc = encodeURIComponent(projectId);

  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtTime = iso => iso ? new Date(iso).toLocaleString() : "—";
  const fmtSize = n => n == null ? "—" : (n / 1024 / 1024).toFixed(1) + " MB";

  async function loadMetrics() {
    try {
      const r = await fetch("/api/metrics");
      const m = await r.json();
      document.getElementById("m-open-orders").textContent = m.orders.open;
      document.getElementById("m-errors").textContent = m.events.errors_last_60m;
      document.getElementById("m-events").textContent = m.events.last_5m;
      document.getElementById("m-backup").textContent = m.backups.last_status || "—";
    } catch (e) { console.error(e); }
  }

  async function loadOrders() {
    const tbody = document.querySelector("#orders-table tbody");
    try {
      const r = await fetch(`/api/projects/${enc}/orders?limit=50`);
      const rows = await r.json();
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="10" class="empty">No orders tracked yet.</td></tr>';
        return;
      }
      tbody.innerHTML = rows.map(o => {
        const cls = ["filled", "partially_filled"].includes(o.status) ? "pl-pos"
                  : ["rejected", "canceled", "cancelled"].includes(o.status) ? "pl-neg" : "";
        return `<tr>
          <td>${fmtTime(o.submitted_at)}</td>
          <td><strong>${esc(o.symbol)}</strong></td>
          <td>${esc(o.side)}</td>
          <td>${esc(o.order_type)}</td>
          <td>${o.qty}</td>
          <td>${o.limit_price != null ? '$' + o.limit_price : "—"}</td>
          <td class="${cls}">${esc(o.status)}</td>
          <td>${o.filled_qty}</td>
          <td>${o.filled_avg_price != null ? '$' + o.filled_avg_price : "—"}</td>
          <td class="muted">${fmtTime(o.last_polled_at)}</td>
        </tr>`;
      }).join("");
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="10" class="empty pl-neg">Error: ${esc(e.message)}</td></tr>`;
    }
  }

  async function loadRecon() {
    const tbody = document.querySelector("#recon-table tbody");
    try {
      const r = await fetch(`/api/projects/${enc}/reconciliation/history?limit=20`);
      const rows = await r.json();
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="empty">No runs yet.</td></tr>';
        return;
      }
      tbody.innerHTML = rows.map(r => `
        <tr>
          <td>${fmtTime(r.ran_at)}</td>
          <td class="${r.mismatches > 0 ? 'pl-neg' : ''}">${r.mismatches}</td>
          <td>${r.auto_sync ? "✓" : "—"}</td>
          <td><button class="btn small ghost" data-recon='${JSON.stringify(r.details).replace(/'/g, '&#39;')}'>Details</button></td>
        </tr>`).join("");
      tbody.querySelectorAll("[data-recon]").forEach(b =>
        b.addEventListener("click", () => toast.info(b.dataset.recon)));
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="4" class="empty pl-neg">Error: ${esc(e.message)}</td></tr>`;
    }
  }

  async function loadBackups() {
    const tbody = document.querySelector("#backup-table tbody");
    try {
      const r = await fetch("/api/backups?limit=20");
      const rows = await r.json();
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="empty">No backups yet.</td></tr>';
        return;
      }
      tbody.innerHTML = rows.map(b => {
        const cls = b.status === "COMPLETE" ? "pl-pos"
                  : b.status === "FAILED" ? "pl-neg" : "";
        return `<tr>
          <td>${fmtTime(b.started_at)}</td>
          <td class="${cls}">${esc(b.status)}</td>
          <td><code style="font-size:11px;">${esc(b.path || "")}</code></td>
          <td>${fmtSize(b.size_bytes)}</td>
          <td class="${b.error_message ? 'pl-neg' : ''}">${esc(b.error_message || "—")}</td>
        </tr>`;
      }).join("");
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty pl-neg">Error: ${esc(e.message)}</td></tr>`;
    }
  }

  async function refreshAll() {
    loadMetrics(); loadOrders(); loadRecon(); loadBackups();
  }

  document.getElementById("rel-refresh").addEventListener("click", refreshAll);
  document.getElementById("orders-poll").addEventListener("click", async () => {
    const r = await fetch(`/api/projects/${enc}/orders/poll`, { method: "POST" });
    const out = await r.json();
    toast.ok(`Updated ${out.updated || 0} order(s)`);
    loadOrders(); loadMetrics();
  });
  document.getElementById("recon-run").addEventListener("click", async () => {
    const auto = document.getElementById("recon-auto").checked;
    const r = await fetch(`/api/projects/${enc}/reconciliation/run?auto_sync=${auto}`, { method: "POST" });
    const out = await r.json();
    const msg = `Reconciled — ${(out.mismatches || []).length} mismatch(es)`;
    if (out.error) toast.error(msg + " · " + out.error);
    else toast.ok(msg);
    loadRecon(); loadMetrics();
  });
  document.getElementById("backup-run").addEventListener("click", async () => {
    if (!await confirmModal("Run a full SQL Server backup now?",
        { title: "Run backup", okLabel: "Run", danger: false })) return;
    const r = await fetch("/api/backups/run", { method: "POST" });
    const out = await r.json();
    if (out.error) toast.error("Backup failed: " + out.error);
    else toast.ok("Backup complete: " + (out.path || ""));
    loadBackups(); loadMetrics();
  });
  document.getElementById("backup-prune").addEventListener("click", async () => {
    if (!await confirmModal("Delete .bak files older than retention period?",
        { title: "Prune backups", okLabel: "Delete" })) return;
    const r = await fetch("/api/backups/prune", { method: "POST" });
    const out = await r.json();
    toast.ok(`Removed ${out.removed} file(s)`);
    loadBackups();
  });

  refreshAll();
  setInterval(refreshAll, 30000);
})();
