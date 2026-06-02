// Backtest page — submit a run, list history, show detail.

(function () {
  const root = document.getElementById("bt-root");
  const projectId = root.dataset.projectId;
  const enc = encodeURIComponent(projectId);

  const fmt = v => (v == null ? "—" :
    Number(v).toLocaleString("en-US", { style: "currency", currency: "USD" }));
  const escapeHtml = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function loadRuns() {
    try {
      const r = await fetch(`/api/projects/${enc}/backtest/runs?limit=25`);
      const runs = await r.json();
      const tbody = document.querySelector("#bt-runs tbody");
      if (!runs.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty">No runs yet.</td></tr>';
        return;
      }
      tbody.innerHTML = runs.map(run => `
        <tr>
          <td>#${run.run_id}</td>
          <td>${escapeHtml(run.name)}</td>
          <td>${run.from_date}</td>
          <td>${run.to_date}</td>
          <td><span class="badge ${run.status === 'COMPLETE' ? 'ok' : 'warn'}">${escapeHtml(run.status)}</span></td>
          <td data-run-pnl="${run.run_id}">—</td>
          <td data-run-trades="${run.run_id}">—</td>
          <td><button class="btn small ghost" data-detail="${run.run_id}">Details</button></td>
        </tr>
      `).join("");
      tbody.querySelectorAll("[data-detail]").forEach(b =>
        b.addEventListener("click", () => loadDetail(parseInt(b.dataset.detail, 10)))
      );
      // Fetch detail summaries for completed runs
      runs.filter(r => r.status === "COMPLETE").forEach(async (run) => {
        const d = await fetch(`/api/projects/${enc}/backtest/runs/${run.run_id}`).then(r => r.json());
        const result = d.result || {};
        document.querySelector(`[data-run-pnl="${run.run_id}"]`).textContent = fmt(result.total_pnl);
        document.querySelector(`[data-run-trades="${run.run_id}"]`).textContent = result.trade_count ?? "—";
      });
    } catch (e) {
      document.querySelector("#bt-runs tbody").innerHTML =
        `<tr><td colspan="8" class="empty">Error: ${escapeHtml(e.message)}</td></tr>`;
    }
  }

  async function loadDetail(runId) {
    document.getElementById("bt-detail-panel").hidden = false;
    document.getElementById("bt-detail-title").textContent = `Run #${runId}`;
    const host = document.getElementById("bt-detail");
    host.innerHTML = "loading…";
    try {
      const r = await fetch(`/api/projects/${enc}/backtest/runs/${runId}`);
      const d = await r.json();
      const result = d.result || {};
      const params = d.params || {};
      host.innerHTML = `
        <div><strong>Window:</strong> ${d.from_date} to ${d.to_date}</div>
        <div><strong>Total P/L:</strong> <span class="${(result.total_pnl ?? 0) >= 0 ? 'pl-pos' : 'pl-neg'}">${fmt(result.total_pnl)}</span></div>
        <div><strong>Trades:</strong> ${result.trade_count ?? 0} (${result.wins ?? 0}W / ${result.losses ?? 0}L)</div>
        <div><strong>Win rate:</strong> ${((result.win_rate ?? 0) * 100).toFixed(1)}%</div>
        <div><strong>Avg P/L:</strong> ${fmt(result.avg_pnl)}</div>
        <div><strong>Params:</strong> <code style="font-size:11px;">${escapeHtml(JSON.stringify(params))}</code></div>
        <h3 style="margin-top: 18px;">Sample trades</h3>
        <table class="data">
          <thead><tr><th>Date</th><th>Ticker</th><th>Strike</th><th>Premium</th>
                     <th>Expiry</th><th>Exp close</th><th>P/L</th><th>Outcome</th></tr></thead>
          <tbody>
            ${(result.trades_sample || []).map(t => `
              <tr>
                <td>${t.date}</td>
                <td><strong>${escapeHtml(t.ticker)}</strong></td>
                <td>${fmt(t.strike)}</td>
                <td>${fmt(t.premium)}</td>
                <td>${t.expiry}</td>
                <td>${fmt(t.exp_close)}</td>
                <td class="${t.pnl >= 0 ? 'pl-pos' : 'pl-neg'}">${fmt(t.pnl)}</td>
                <td>${escapeHtml(t.outcome)}</td>
              </tr>`).join("")}
          </tbody>
        </table>`;
    } catch (e) {
      host.textContent = "error: " + e.message;
    }
  }

  document.getElementById("bt-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const universeStr = (fd.get("universe") || "").toString().trim();
    const payload = {
      name: fd.get("name"),
      from_date: fd.get("from_date"),
      to_date: fd.get("to_date"),
      universe: universeStr
        ? universeStr.split(",").map(s => s.trim().toUpperCase()).filter(Boolean)
        : null,
    };
    const status = document.getElementById("bt-status");
    const btn = document.getElementById("bt-run");
    btn.disabled = true;
    status.textContent = "running…";
    try {
      const r = await fetch(`/api/projects/${enc}/backtest/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const out = await r.json();
      if (!r.ok) {
        status.textContent = "error: " + (out.detail || r.statusText);
      } else {
        status.textContent = `run #${out.run_id} complete — P/L ${fmt(out.summary?.total_pnl)}`;
        loadRuns();
      }
    } catch (e) {
      status.textContent = "error: " + e.message;
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("bt-detail-close").addEventListener("click", () =>
    document.getElementById("bt-detail-panel").hidden = true
  );

  // Set sensible default dates
  const today = new Date();
  const monthAgo = new Date(today.getTime() - 30 * 86400000);
  document.querySelector('[name="from_date"]').value = monthAgo.toISOString().slice(0, 10);
  document.querySelector('[name="to_date"]').value = today.toISOString().slice(0, 10);

  loadRuns();
})();
