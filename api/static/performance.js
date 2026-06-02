// Performance page — cards + equity curve + closed trades table.

(function () {
  const root = document.getElementById("perf-root");
  const projectId = root.dataset.projectId;
  const enc = encodeURIComponent(projectId);

  const els = {
    period:   document.getElementById("perf-period"),
    refresh:  document.getElementById("perf-refresh"),
    realized: document.getElementById("card-realized"),
    rsub:     document.getElementById("card-realized-sub"),
    unreal:   document.getElementById("card-unrealized"),
    winrate:  document.getElementById("card-winrate"),
    trades:   document.getElementById("card-trades"),
    premium:  document.getElementById("card-premium"),
    avgWin:   document.getElementById("card-avg-win"),
    avgLoss:  document.getElementById("card-avg-loss"),
    pf:       document.getElementById("card-pf"),
    dd:       document.getElementById("card-dd"),
    curveSum: document.getElementById("curve-summary"),
    canvas:   document.getElementById("equity-curve"),
    tbody:    document.querySelector("#closed-trades tbody"),
    tcount:   document.getElementById("trades-count"),
  };

  const fmtMoney = (v, opts = {}) => {
    if (v === null || v === undefined) return "—";
    const n = typeof v === "number" ? v : parseFloat(v);
    if (isNaN(n)) return "—";
    const sign = opts.sign && n > 0 ? "+" : "";
    return sign + n.toLocaleString("en-US", {
      style: "currency", currency: "USD", maximumFractionDigits: 2,
    });
  };
  const fmtPct = v => (v == null ? "—" : `${(v * 100).toFixed(1)}%`);
  const fmtTime = iso => {
    if (!iso) return "";
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString();
  };
  const colorPL = v => (v > 0 ? "pl-pos" : v < 0 ? "pl-neg" : "");
  const escapeHtml = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function loadSummary() {
    const period = els.period.value;
    try {
      const r = await fetch(`/api/projects/${enc}/performance/summary?period=${period}`);
      const s = await r.json();
      els.realized.textContent = fmtMoney(s.realized_pnl, { sign: true });
      els.realized.className = "card-value " + colorPL(s.realized_pnl);
      els.rsub.textContent = `${s.trade_count} closed · since ${s.since?.slice(0, 10)}`;
      els.unreal.textContent = fmtMoney(s.unrealized_pnl, { sign: true });
      els.unreal.className = "card-value " + colorPL(s.unrealized_pnl);
      els.winrate.textContent = fmtPct(s.win_rate);
      els.trades.textContent = `${s.wins}W · ${s.losses}L`;
      els.premium.textContent = fmtMoney(s.total_premium_captured);
      els.avgWin.textContent = fmtMoney(s.avg_winner);
      els.avgLoss.textContent = fmtMoney(s.avg_loser);
      els.pf.textContent = s.profit_factor == null ? "∞" : s.profit_factor.toFixed(2);
      els.dd.textContent = fmtMoney(s.max_drawdown);
      els.dd.className = "card-value pl-neg";
    } catch (e) {
      els.realized.textContent = "err";
    }
  }

  async function loadCurve() {
    const period = els.period.value;
    try {
      const r = await fetch(`/api/projects/${enc}/performance/equity_curve?period=${period}`);
      const pts = await r.json();
      drawCurve(pts);
      els.curveSum.textContent = `${pts.length} snapshot(s)`;
    } catch (e) {
      els.curveSum.textContent = "err: " + e.message;
    }
  }

  function drawCurve(points) {
    const c = els.canvas;
    const ctx = c.getContext("2d");
    const w = c.clientWidth || c.width;
    const h = c.clientHeight || c.height;
    c.width = w; c.height = h;
    ctx.clearRect(0, 0, w, h);

    if (!points.length) {
      ctx.fillStyle = "#9a948a";
      ctx.font = "13px Inter, system-ui";
      ctx.textAlign = "center";
      ctx.fillText("No snapshots yet — they'll begin once cycles run.", w / 2, h / 2);
      return;
    }

    const values = points.map(p => p.equity);
    const minV = Math.min(...values);
    const maxV = Math.max(...values);
    const range = maxV - minV || 1;
    const pad = 30;

    // axes
    ctx.strokeStyle = "#ebe3d4";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(pad, 5); ctx.lineTo(pad, h - pad);
    ctx.lineTo(w - 5, h - pad);
    ctx.stroke();

    // labels
    ctx.fillStyle = "#9a948a";
    ctx.font = "10px Inter, system-ui";
    ctx.textAlign = "right";
    ctx.fillText("$" + maxV.toFixed(0), pad - 4, 12);
    ctx.fillText("$" + minV.toFixed(0), pad - 4, h - pad);

    // line
    ctx.strokeStyle = "#7e8fc1";
    ctx.lineWidth = 2;
    ctx.beginPath();
    points.forEach((p, i) => {
      const x = pad + ((w - pad - 10) * i) / Math.max(1, points.length - 1);
      const y = (h - pad) - ((p.equity - minV) / range) * (h - pad - 10);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();

    // fill below
    ctx.lineTo(w - 5, h - pad);
    ctx.lineTo(pad, h - pad);
    ctx.fillStyle = "rgba(126, 143, 193, 0.14)";
    ctx.fill();
  }

  async function loadTrades() {
    try {
      const r = await fetch(`/api/projects/${enc}/performance/closed_trades?limit=200`);
      const trades = await r.json();
      els.tcount.textContent = `${trades.length} shown`;
      if (!trades.length) {
        els.tbody.innerHTML = '<tr><td colspan="10" class="empty">No closed trades yet.</td></tr>';
        return;
      }
      els.tbody.innerHTML = trades.map(t => `
        <tr>
          <td>${escapeHtml(fmtTime(t.closed_at))}</td>
          <td><strong>${escapeHtml(t.ticker)}</strong></td>
          <td>${escapeHtml(t.strategy_phase)}</td>
          <td>${fmtMoney(t.strike_price)}</td>
          <td>${t.quantity}</td>
          <td>${t.days_held}</td>
          <td>${fmtMoney(t.premium_collected)}</td>
          <td>${fmtMoney(t.close_cost)}</td>
          <td class="${colorPL(t.realized_pnl)}">${fmtMoney(t.realized_pnl, { sign: true })}</td>
          <td><span class="badge ghost">${escapeHtml(t.closure_reason)}</span></td>
        </tr>
      `).join("");
    } catch (e) {
      els.tcount.textContent = "err: " + e.message;
    }
  }

  function refreshAll() {
    loadSummary();
    loadCurve();
    loadTrades();
  }

  els.period.addEventListener("change", refreshAll);
  els.refresh.addEventListener("click", refreshAll);
  refreshAll();
  setInterval(refreshAll, 30000);
})();
