(function () {
  const fmt = v => v == null ? "—" : Number(v).toLocaleString("en-US", { style: "currency", currency: "USD" });
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pl = v => v > 0 ? "pl-pos" : v < 0 ? "pl-neg" : "";

  async function load() {
    try {
      const r = await fetch("/api/portfolio");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const d = await r.json();
      document.getElementById("tot-equity").textContent = fmt(d.totals.equity);
      document.getElementById("tot-unrealized").textContent = fmt(d.totals.unrealized_pnl);
      document.getElementById("tot-unrealized").className = "card-value " + pl(d.totals.unrealized_pnl);
      document.getElementById("tot-realized").textContent = fmt(d.totals.realized_pnl_month);
      document.getElementById("tot-realized").className = "card-value " + pl(d.totals.realized_pnl_month);
      document.getElementById("tot-active").textContent = `${d.totals.active_count}/${d.totals.project_count}`;

      const tbody = document.querySelector("#port-table tbody");
      if (!d.projects.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty">No projects.</td></tr>';
        return;
      }
      tbody.innerHTML = d.projects.map(p => `
        <tr>
          <td><strong><a href="/projects/${encodeURIComponent(p.project_id)}">${esc(p.project_name)}</a></strong></td>
          <td>${p.is_active ? "✓" : "—"}</td>
          <td>${fmt(p.equity)}</td>
          <td class="${pl(p.unrealized_pnl)}">${fmt(p.unrealized_pnl)}</td>
          <td class="${pl(p.realized_pnl_month)}">${fmt(p.realized_pnl_month)}</td>
          <td>${p.trade_count_month}</td>
          <td>${p.win_rate != null ? (p.win_rate * 100).toFixed(1) + "%" : "—"}</td>
        </tr>`).join("");
    } catch (e) {
      document.querySelector("#port-table tbody").innerHTML =
        `<tr><td colspan="7" class="empty pl-neg">Error: ${esc(e.message)}</td></tr>`;
    }
  }
  document.getElementById("port-refresh").addEventListener("click", load);
  load();
  setInterval(load, 30000);
})();
