(function () {
  const root = document.getElementById("cycles-root");
  const projectId = root.dataset.projectId;
  const enc = encodeURIComponent(projectId);

  const fmt = v => (v == null ? "—" :
    Number(v).toLocaleString("en-US", { style: "currency", currency: "USD" }));
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtTime = iso => iso ? new Date(iso).toLocaleString() : "—";
  const pl = v => (v > 0 ? "pl-pos" : v < 0 ? "pl-neg" : "");

  async function load() {
    const filter = document.getElementById("cycle-filter").value;
    const url = filter
      ? `/api/projects/${enc}/cycles?status=${filter}&limit=200`
      : `/api/projects/${enc}/cycles?limit=200`;
    try {
      const r = await fetch(url);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const rows = await r.json();
      renderCards(rows);
      renderTable(rows);
    } catch (e) {
      document.querySelector("#cycles-table tbody").innerHTML =
        `<tr><td colspan="12" class="empty pl-neg">Error: ${esc(e.message)}</td></tr>`;
    }
  }

  function renderCards(rows) {
    const open = rows.filter(r => r.status === "OPEN").length;
    const closed = rows.filter(r => r.status === "CLOSED").length;
    const premium = rows.reduce((s, r) => s + (r.total_premium || 0), 0);
    const pnl = rows.reduce((s, r) => s + (r.realized_pnl || 0), 0);
    document.getElementById("card-open").textContent = open;
    document.getElementById("card-closed").textContent = closed;
    document.getElementById("card-premium").textContent = fmt(premium);
    document.getElementById("card-pnl").textContent = fmt(pnl);
    document.getElementById("card-pnl").className = "card-value " + pl(pnl);
  }

  function renderTable(rows) {
    const tbody = document.querySelector("#cycles-table tbody");
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="12" class="empty">No wheel cycles yet — they appear when the first CSP is sold.</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(c => `
      <tr>
        <td><strong>${esc(c.ticker)}</strong></td>
        <td><span class="badge ${c.status === 'OPEN' ? 'ok' : 'ghost'}">${esc(c.status)}</span></td>
        <td>${fmtTime(c.started_at)}</td>
        <td>${c.days_open ?? "—"}</td>
        <td>${c.csp_count}</td>
        <td>${c.assignment_count}</td>
        <td>${c.cc_count}</td>
        <td>${fmt(c.total_premium)}</td>
        <td>${c.cost_basis_adjusted != null ? fmt(c.cost_basis_adjusted) : "—"}</td>
        <td class="${pl(c.realized_pnl)}">${fmt(c.realized_pnl)}</td>
        <td>${esc(c.final_outcome || "—")}</td>
        <td><button class="btn small ghost" data-cycle="${c.cycle_id}">View</button></td>
      </tr>`).join("");
    tbody.querySelectorAll("[data-cycle]").forEach(b =>
      b.addEventListener("click", () => detail(parseInt(b.dataset.cycle, 10)))
    );
  }

  async function detail(cid) {
    document.getElementById("cycle-detail-panel").hidden = false;
    document.getElementById("cycle-detail-title").textContent = `Cycle #${cid}`;
    const host = document.getElementById("cycle-detail");
    host.innerHTML = "loading…";
    try {
      const c = await (await fetch(`/api/projects/${enc}/cycles/${cid}`)).json();
      host.innerHTML = `
        <div><strong>Ticker:</strong> ${esc(c.ticker)}</div>
        <div><strong>Status:</strong> ${esc(c.status)} ${c.final_outcome ? "(" + esc(c.final_outcome) + ")" : ""}</div>
        <div><strong>Started:</strong> ${fmtTime(c.started_at)}${c.ended_at ? "  ·  ended " + fmtTime(c.ended_at) : ""}</div>
        <div><strong>Total premium:</strong> ${fmt(c.total_premium)}</div>
        <div><strong>Realized P/L:</strong> <span class="${pl(c.realized_pnl)}">${fmt(c.realized_pnl)}</span></div>
        <div><strong>Adjusted cost basis:</strong> ${c.cost_basis_adjusted != null ? fmt(c.cost_basis_adjusted) : "—"}</div>
        <h3 style="margin-top:18px;">Contracts (${c.contracts.length})</h3>
        <table class="data">
          <thead><tr><th>Phase</th><th>Symbol</th><th>Strike</th><th>Premium</th>
                     <th>Exp</th><th>Qty</th><th>Closed</th><th>Assigned</th></tr></thead>
          <tbody>
            ${c.contracts.map(x => `
              <tr>
                <td><span class="badge ghost">${esc(x.strategy_phase)}</span></td>
                <td><code style="font-size:11px">${esc(x.option_symbol || "")}</code></td>
                <td>${fmt(x.strike_price)}</td>
                <td>${fmt(x.premium_collected)}</td>
                <td>${esc(x.expiration_date)}</td>
                <td>${x.quantity}</td>
                <td>${x.is_closed ? "✓" : "—"}</td>
                <td>${x.is_assigned ? "✓" : "—"}</td>
              </tr>`).join("")}
          </tbody>
        </table>`;
    } catch (e) {
      host.textContent = "error: " + e.message;
    }
  }

  document.getElementById("cycle-filter").addEventListener("change", load);
  document.getElementById("cycle-refresh").addEventListener("click", load);
  document.getElementById("cycle-detail-close").addEventListener("click", () =>
    document.getElementById("cycle-detail-panel").hidden = true);

  load();
  setInterval(load, 30000);
})();
