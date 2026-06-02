// Analysis page — per-ticker performance + attribution.

(function () {
  const root = document.getElementById("analysis-root");
  const projectId = root.dataset.projectId;
  const enc = encodeURIComponent(projectId);

  const fmt = v => (v == null ? "—" :
    Number(v).toLocaleString("en-US", { style: "currency", currency: "USD" }));
  const fmtPct = v => (v == null ? "—" : `${(v * 100).toFixed(1)}%`);
  const colorPL = v => (v > 0 ? "pl-pos" : v < 0 ? "pl-neg" : "");
  const escapeHtml = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function loadHeatmap() {
    const minTrades = document.getElementById("min-trades").value;
    const sinceDays = document.getElementById("since-days").value;
    try {
      const r = await fetch(
        `/api/projects/${enc}/performance/by_ticker?since_days=${sinceDays}&min_trades=${minTrades}`
      );
      const rows = await r.json();
      const tbody = document.querySelector("#heatmap-table tbody");
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="9" class="empty">No closed trades match these filters.</td></tr>';
        return;
      }
      tbody.innerHTML = rows.map(r => `
        <tr>
          <td><strong>${escapeHtml(r.ticker)}</strong></td>
          <td>${r.trade_count}</td>
          <td>${fmtPct(r.win_rate)}</td>
          <td class="${colorPL(r.total_pnl)}">${fmt(r.total_pnl)}</td>
          <td class="${colorPL(r.avg_pnl)}">${fmt(r.avg_pnl)}</td>
          <td>${r.avg_days_held.toFixed(1)}</td>
          <td>${fmt(r.total_premium)}</td>
          <td class="pl-pos">${fmt(r.biggest_win)}</td>
          <td class="pl-neg">${fmt(r.biggest_loss)}</td>
        </tr>
      `).join("");
    } catch (e) {
      document.querySelector("#heatmap-table tbody").innerHTML =
        `<tr><td colspan="9" class="empty">Error: ${escapeHtml(e.message)}</td></tr>`;
    }
  }

  async function loadAttribution() {
    const dim = document.getElementById("attr-dim").value;
    try {
      const r = await fetch(
        `/api/projects/${enc}/performance/attribution?dimension=${dim}&since_days=365&min_trades=1`
      );
      const rows = await r.json();
      const host = document.getElementById("attr-bars");
      if (!rows.length) {
        host.innerHTML = '<div class="empty" style="text-align:center; padding:20px; color:var(--muted)">No data for this dimension yet.</div>';
        return;
      }
      const maxAbs = Math.max(...rows.map(r => Math.abs(r.total_pnl))) || 1;
      host.innerHTML = rows.map(r => {
        const w = Math.round((Math.abs(r.total_pnl) / maxAbs) * 60);
        const cls = r.total_pnl >= 0 ? "attr-pos" : "attr-neg";
        return `
          <div class="attr-row">
            <div class="attr-label">${escapeHtml(r.label)} <span class="muted">(${r.trade_count})</span></div>
            <div class="attr-bar">
              <div class="attr-bar-fill ${cls}" style="width:${w}%"></div>
            </div>
            <div class="attr-stats">
              <span class="${colorPL(r.total_pnl)}">${fmt(r.total_pnl)}</span>
              <span class="muted"> · ${fmtPct(r.win_rate)} · avg ${fmt(r.avg_pnl)} · ${r.confidence}</span>
            </div>
          </div>`;
      }).join("");
    } catch (e) {
      document.getElementById("attr-bars").innerHTML =
        `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
    }
  }

  document.getElementById("heatmap-refresh").addEventListener("click", loadHeatmap);
  document.getElementById("attr-refresh").addEventListener("click", loadAttribution);
  document.getElementById("attr-dim").addEventListener("change", loadAttribution);

  loadHeatmap();
  loadAttribution();
})();
