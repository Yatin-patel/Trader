// Market Outlook — top performers + 30/60/90-day predictions

(function () {
  const root = document.getElementById("outlook-root");
  const projectId = root.dataset.projectId;
  const enc = encodeURIComponent(projectId);

  const fmtMoney = (v) => {
    if (v === null || v === undefined) return "—";
    const n = typeof v === "number" ? v : parseFloat(v);
    if (isNaN(n)) return "—";
    return n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  };
  const fmtPct = (v) => {
    if (v === null || v === undefined) return "—";
    const n = typeof v === "number" ? v : parseFloat(v);
    if (isNaN(n)) return "—";
    const cls = n > 0 ? "pl-pos" : n < 0 ? "pl-neg" : "";
    return `<span class="${cls}">${(n * 100).toFixed(1)}%</span>`;
  };
  const fmtIv = (v) => {
    if (v === null || v === undefined) return "—";
    const n = typeof v === "number" ? v : parseFloat(v);
    if (isNaN(n)) return "—";
    const pct = (n * 100).toFixed(0);
    return `<span class="badge ${n >= 0.5 ? "ok" : "ghost"}">${pct}</span>`;
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let selectedRow = null;

  async function loadPerformers() {
    const tbody = document.querySelector("#performers-table tbody");
    tbody.innerHTML = '<tr><td colspan="12" class="empty">Loading rankings… (first load may take 30-60s)</td></tr>';
    try {
      const r = await fetch(`/api/projects/${enc}/outlook/top_performers?limit=25`, { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const rows = await r.json();
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="12" class="empty">No tickers ranked yet. Wait until the scanner has data.</td></tr>';
        return;
      }
      const universeSize = rows[0]._universe_size;
      const rankedSize = rows[0]._ranked_size;
      const meta = document.getElementById("ranking-meta");
      if (meta && universeSize) {
        meta.textContent = `Showing top ${rows.length} of ${rankedSize} ranked tickers · universe ${universeSize}`;
      }
      tbody.innerHTML = rows.map((r, i) => `
        <tr class="perf-row" data-ticker="${esc(r.ticker)}">
          <td class="muted">${i + 1}</td>
          <td><strong>${esc(r.ticker)}</strong></td>
          <td>${r.last_price ? fmtMoney(r.last_price) : "—"}</td>
          <td>${fmtPct(r.mom_1m)}</td>
          <td>${fmtPct(r.mom_3m)}</td>
          <td>${fmtPct(r.mom_6m)}</td>
          <td>${fmtIv(r.iv_rank)}</td>
          <td>${r.realized_pnl ? fmtMoney(r.realized_pnl) : "—"}</td>
          <td>${r.win_rate !== null && r.win_rate !== undefined ? (r.win_rate * 100).toFixed(0) + "%" : "—"}</td>
          <td>${r.n_cycles ?? 0}</td>
          <td><strong>${(r.score || 0).toFixed(3)}</strong></td>
          <td><button class="btn small ghost" data-pred="${esc(r.ticker)}">Outlook →</button></td>
        </tr>
      `).join("");
      tbody.querySelectorAll(".perf-row").forEach(tr => {
        tr.addEventListener("click", (e) => {
          if (e.target.closest("button")) return;
          showOutlook(tr.dataset.ticker, tr);
        });
      });
      tbody.querySelectorAll("[data-pred]").forEach(b => {
        b.addEventListener("click", (e) => {
          e.stopPropagation();
          const tr = b.closest("tr");
          showOutlook(b.dataset.pred, tr);
        });
      });
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="12" class="empty">Failed to load: ${esc(e.message)}</td></tr>`;
    }
  }

  async function showOutlook(ticker, rowEl) {
    if (selectedRow) selectedRow.classList.remove("selected");
    if (rowEl) { rowEl.classList.add("selected"); selectedRow = rowEl; }

    const panel = document.getElementById("outlook-detail-panel");
    const host = document.getElementById("outlook-detail");
    const title = document.getElementById("outlook-ticker-title");
    const meta = document.getElementById("outlook-meta");
    panel.hidden = false;
    title.textContent = `${ticker} · Outlook`;
    meta.textContent = "Loading 30 / 60 / 90-day projections…";
    host.innerHTML = '<div class="muted">Computing quant bands and asking the LLM. First request can take 10-30s.</div>';
    panel.scrollIntoView({ behavior: "smooth", block: "start" });

    try {
      const r = await fetch(`/api/projects/${enc}/outlook/predict/${encodeURIComponent(ticker)}`, { cache: "no-store" });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || ("HTTP " + r.status));
      }
      const data = await r.json();
      renderOutlook(data, host, meta);
    } catch (e) {
      host.innerHTML = `<div class="pl-neg">Error: ${esc(e.message)}</div>`;
      meta.textContent = "";
    }
  }

  function renderOutlook(data, host, meta) {
    const horizons = data.horizons || {};
    const ctx = data.context || {};
    meta.textContent = `Spot: $${(ctx.spot_price ?? 0).toFixed(2)} · source: ${data.source || "?"}`;

    const cards = ["30", "60", "90"].map(h => {
      const hz = horizons[h] || {};
      const q = hz.quant || {};
      const dir = (hz.direction || "").toLowerCase();
      const dirCls = dir === "bullish" ? "dir-bullish" : dir === "bearish" ? "dir-bearish" : "dir-neutral";
      const conf = (hz.confidence || "").toLowerCase();
      const confCls = conf === "high" ? "conf-high" : conf === "medium" ? "conf-medium" : "conf-low";

      return `
        <div class="horizon-card">
          <h3>${h}-Day Outlook</h3>
          <div class="horizon-band">
            <div class="band-cell">
              <div class="band-label">P10 Low</div>
              <div class="band-value">${q.low !== undefined ? "$" + q.low.toFixed(2) : "—"}</div>
            </div>
            <div class="band-cell">
              <div class="band-label">Median</div>
              <div class="band-value">${q.mid !== undefined ? "$" + q.mid.toFixed(2) : "—"}</div>
            </div>
            <div class="band-cell">
              <div class="band-label">P90 High</div>
              <div class="band-value">${q.high !== undefined ? "$" + q.high.toFixed(2) : "—"}</div>
            </div>
          </div>
          <div class="quant-foot">
            Expected return: <strong>${q.expected_return_pct !== undefined ? q.expected_return_pct.toFixed(2) + "%" : "—"}</strong>
            · Prob &gt; spot: <strong>${q.prob_up !== undefined ? (q.prob_up * 100).toFixed(0) + "%" : "—"}</strong>
            · σ (ann.): <strong>${q.annualized_vol !== undefined ? (q.annualized_vol * 100).toFixed(1) + "%" : "—"}</strong>
          </div>
          ${hz.narrative ? `<div class="horizon-narrative">${esc(hz.narrative)}</div>` : ""}
          <div style="margin-top: 10px;">
            ${dir ? `<span class="${dirCls}">${esc(dir.toUpperCase())}</span>` : ""}
            ${conf ? ` <span class="conf-pill ${confCls}">${esc(conf)}</span>` : ""}
          </div>
        </div>`;
    }).join("");

    const contextLine = `
      <div class="muted" style="font-size: 12px; margin-bottom: 14px;">
        IV rank: <strong>${ctx.iv_rank !== null && ctx.iv_rank !== undefined ? (ctx.iv_rank * 100).toFixed(0) + "%" : "—"}</strong>
        · Wheel history: <strong>${(ctx.wheel_history && ctx.wheel_history.n_cycles) || 0} cycles</strong>
        ${ctx.wheel_history && ctx.wheel_history.realized_pnl !== undefined ? ", " + fmtMoney(ctx.wheel_history.realized_pnl) + " realized" : ""}
        · 1M / 3M / 6M momentum:
        ${fmtPct(ctx.momentum && ctx.momentum.mom_1m)} /
        ${fmtPct(ctx.momentum && ctx.momentum.mom_3m)} /
        ${fmtPct(ctx.momentum && ctx.momentum.mom_6m)}
      </div>`;

    host.innerHTML = contextLine + `<div class="horizon-grid">${cards}</div>`;
  }

  document.getElementById("outlook-refresh").addEventListener("click", loadPerformers);

  loadPerformers();
})();
