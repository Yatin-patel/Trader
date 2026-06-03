// Live project dashboard — polls /api/projects/{id}/snapshot every 5s
// and renders cards, pipeline, timeline, positions, contracts, and warnings.

(function () {
  const root = document.getElementById("project-root");
  const projectId = root.dataset.projectId;
  const projectIdEnc = encodeURIComponent(projectId);

  const fmtMoney = (v) => {
    if (v === null || v === undefined || v === "") return "—";
    const n = typeof v === "number" ? v : parseFloat(v);
    if (isNaN(n)) return String(v);
    return n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  };
  // All timestamps in the UI are rendered in US/Eastern (the market's
  // timezone) so the user doesn't have to translate UTC or their local
  // timezone into market time when scanning recent activity. DST is
  // handled automatically by Intl.DateTimeFormat.
  const ET_TZ = "America/New_York";
  const fmtET = (d, opts) => d.toLocaleString("en-US", { timeZone: ET_TZ, ...(opts || {}) });
  const fmtTime = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const now = new Date();
    const diff = (now - d) / 1000;
    if (diff < 60) return `${Math.floor(diff)}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    // Anything older than an hour: show full ET timestamp so the user
    // can correlate with the market clock.
    return fmtET(d, {
      month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
      hour12: false, timeZoneName: "short",
    });
  };

  const PIPE_NODES = ["Scanner", "Strategist", "Guardrail", "Executor"];

  function renderBadges(snap) {
    const mb = document.getElementById("market-badge");
    const isOpen = snap.clock && snap.clock.is_open;
    mb.textContent = isOpen ? "MARKET OPEN" : "MARKET CLOSED";
    mb.className = "badge " + (isOpen ? "ok" : "warn");

    const rb = document.getElementById("runner-badge");
    const active = snap.project && snap.project.is_active;
    rb.textContent = active ? "runner: ACTIVE" : "runner: INACTIVE";
    rb.className = "badge " + (active ? "ok" : "danger");

    document.getElementById("last-update").textContent = "updated " + fmtET(new Date(), {
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      hour12: false, timeZoneName: "short",
    });
  }

  function renderAccount(snap) {
    const a = snap.account || {};
    document.getElementById("card-cash").textContent = fmtMoney(a.cash);
    document.getElementById("card-bp").textContent = fmtMoney(a.buying_power);
    document.getElementById("card-equity").textContent = fmtMoney(a.equity);
    document.getElementById("card-pv").textContent = fmtMoney(a.portfolio_value);
  }

  function renderOverview(d) {
    const startEl = document.getElementById("card-starting");
    const startSub = document.getElementById("card-starting-sub");
    const curEl = document.getElementById("card-current");
    const curSub = document.getElementById("card-current-sub");
    const stEl = document.getElementById("card-st-gain");
    const stPct = document.getElementById("card-st-pct");
    const ltEl = document.getElementById("card-lt-gain");
    const ltPct = document.getElementById("card-lt-pct");

    startEl.textContent = fmtMoney(d.starting_balance);
    if (d.starting_at) {
      const dt = new Date(d.starting_at);
      startSub.textContent = "since " + fmtET(dt, {
        month: "short", day: "numeric", year: "numeric",
      });
    } else {
      startSub.textContent = "from project allocation";
    }
    curEl.textContent = fmtMoney(d.current_equity);
    curSub.textContent = "cash: " + fmtMoney(d.current_cash);

    function paintGain(amtEl, pctEl, gain) {
      if (!gain || gain.dollars === null || gain.dollars === undefined) {
        amtEl.textContent = "—";
        amtEl.className = "card-value";
        pctEl.textContent = "no reference point yet";
        pctEl.className = "card-sub muted";
        return;
      }
      const sign = gain.dollars >= 0 ? "+" : "";
      amtEl.textContent = sign + fmtMoney(gain.dollars);
      const pos = gain.dollars > 0;
      const neg = gain.dollars < 0;
      amtEl.className = "card-value " + (pos ? "pl-pos" : neg ? "pl-neg" : "");
      pctEl.textContent = (gain.pct >= 0 ? "+" : "") + gain.pct.toFixed(2) + "%";
      pctEl.className = "card-sub " + (pos ? "pl-pos" : neg ? "pl-neg" : "muted");
    }
    paintGain(stEl, stPct, d.short_term);
    paintGain(ltEl, ltPct, d.long_term);
  }

  async function loadOverview() {
    try {
      const r = await fetch(`/api/projects/${projectIdEnc}/dashboard_overview`,
                            { cache: "no-store" });
      if (!r.ok) return;
      renderOverview(await r.json());
    } catch (e) { /* ignore */ }
  }

  function drawSparkline(canvas, points, opts = {}) {
    if (!canvas || !points || !points.length) return;
    const ctx = canvas.getContext("2d");
    // Resize to actual pixel ratio for crisp rendering.
    const w = canvas.clientWidth || canvas.width;
    const h = canvas.clientHeight || canvas.height;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    const vals = points.map(p => p.equity);
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const range = max - min || 1;
    const pad = 2;
    const ix = (i) => pad + ((w - 2 * pad) * i) / Math.max(1, points.length - 1);
    const iy = (v) => (h - pad) - ((v - min) / range) * (h - 2 * pad);

    // Determine trend color
    const trendUp = points[points.length - 1].equity >= points[0].equity;
    const stroke = opts.color || (trendUp ? "#7eaf8b" : "#c9897f");
    const fill   = opts.fill   || (trendUp ? "rgba(126,175,139,0.18)"
                                           : "rgba(201,137,127,0.18)");

    // Filled area
    ctx.beginPath();
    points.forEach((p, i) => {
      const x = ix(i), y = iy(p.equity);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.lineTo(w - pad, h - pad);
    ctx.lineTo(pad, h - pad);
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();

    // Line
    ctx.beginPath();
    points.forEach((p, i) => {
      const x = ix(i), y = iy(p.equity);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = stroke;
    ctx.lineWidth = 1.4;
    ctx.stroke();
  }

  async function loadSparklines() {
    try {
      const r = await fetch(`/api/projects/${projectIdEnc}/performance/equity_curve?period=month`,
                            { cache: "no-store" });
      if (!r.ok) return;
      const pts = await r.json();
      if (!pts.length) return;
      // Current equity card: full month
      drawSparkline(document.getElementById("spark-current"), pts);
      // Short-term card: last 7 days slice
      const sevenAgo = Date.now() - 7 * 24 * 3600 * 1000;
      const st = pts.filter(p => new Date(p.t).getTime() >= sevenAgo);
      drawSparkline(document.getElementById("spark-st"), st.length ? st : pts);
      // Long-term card: everything we have
      drawSparkline(document.getElementById("spark-lt"), pts);
    } catch (e) { /* ignore */ }
  }

  function renderWarnings(snap) {
    const host = document.getElementById("warnings");
    const list = snap.warnings || [];
    host.innerHTML = list.map(w => {
      const cls = w.level === "error" ? "alert error"
                : w.level === "warn"  ? "alert warn"
                : "alert info";
      return `<div class="${cls}">
                <div class="alert-title">${escapeHtml(w.title)}</div>
                <div class="alert-detail">${escapeHtml(w.detail || "")}</div>
              </div>`;
    }).join("");
  }

  function renderPipeline(snap) {
    const host = document.getElementById("pipeline");
    const p = snap.pipeline || {};
    host.innerHTML = PIPE_NODES.map((name, i) => {
      const node = p[name] || { status: "idle", summary: "Waiting", ts: null, kind: "pending" };
      const isActive = node.status === "active";
      const isError = node.kind === "error" || node.kind === "guardrail-alert";
      const cls = isError ? "pipe-node error"
                : isActive ? "pipe-node active"
                : "pipe-node idle";
      const arrow = i < PIPE_NODES.length - 1 ? '<div class="pipe-arrow">→</div>' : "";
      return `
        <div class="${cls}">
          <div class="pipe-name">${name}</div>
          <div class="pipe-summary">${escapeHtml(node.summary)}</div>
          <div class="pipe-ts muted">${fmtTime(node.ts)}</div>
        </div>${arrow}`;
    }).join("");
  }

  function renderChart(snap) {
    const data = snap.cycles_chart || [];
    const max = Math.max(1, ...data.map(d => d.n));
    const total = data.reduce((s, d) => s + d.n, 0);
    document.getElementById("chart-total").textContent = `${total} cycles total`;
    const host = document.getElementById("cycles-chart");
    host.innerHTML = data.map(d => {
      const h = Math.round((d.n / max) * 100);
      return `<div class="bar" title="${d.t} — ${d.n} cycles">
                <div class="bar-fill" style="height:${h}%"></div>
                <div class="bar-label">${d.t.slice(-2)}</div>
              </div>`;
    }).join("");
  }

  function renderPositions(snap) {
    const tbody = document.querySelector("#positions-table tbody");
    const dbPositions = snap.positions_db || [];
    const live = snap.positions_live || [];
    if (!live.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty">No open positions</td></tr>';
      return;
    }
    const dbBySymbol = {};
    dbPositions.forEach(p => { dbBySymbol[p.ticker] = p.position_id; });
    tbody.innerHTML = live.filter(p => p.asset_class === "us_equity").map(p => {
      const pl = p.unrealized_pl;
      const cls = pl > 0 ? "pl-pos" : pl < 0 ? "pl-neg" : "";
      const pid = dbBySymbol[p.symbol];
      const closeBtn = pid
        ? `<button class="btn small danger" data-close-position="${pid}" data-ticker="${escapeHtml(p.symbol)}">Close</button>`
        : `<button class="btn small danger" data-close-symbol="${escapeHtml(p.symbol)}" title="Untracked — closes via Alpaca">Close*</button>`;
      return `<tr>
        <td><strong>${escapeHtml(p.symbol)}</strong>${pid ? "" : ' <span class="muted" title="Open on Alpaca but not tracked in DB">∗</span>'}</td>
        <td>${p.qty}</td>
        <td>${fmtMoney(p.avg_entry_price)}</td>
        <td>${fmtMoney(p.current_price)}</td>
        <td class="${cls}">${pl !== null && pl !== undefined ? fmtMoney(pl) : "—"}</td>
        <td>${closeBtn}</td>
      </tr>`;
    }).join("") || '<tr><td colspan="6" class="empty">No equity positions</td></tr>';
    tbody.querySelectorAll("[data-close-position]").forEach(b =>
      b.addEventListener("click", () => closePosition(b.dataset.closePosition, b.dataset.ticker))
    );
    tbody.querySelectorAll("[data-close-symbol]").forEach(b =>
      b.addEventListener("click", () => closeAlpacaSymbol(b.dataset.closeSymbol))
    );
  }

  async function closePosition(pid, ticker) {
    if (!await confirmModal(`Close all shares of ${ticker}?`,
        { title: "Close position", okLabel: "Close" })) return;
    const r = await fetch(`/api/projects/${projectIdEnc}/positions/${pid}/close`, { method: "POST" });
    if (r.ok) { tick(); toast.ok("Close submitted"); } else toast.error("Close failed");
  }
  async function closeContract(cid, label) {
    if (!await confirmModal(`Buy to close ${label}?`,
        { title: "Close contract", okLabel: "Close" })) return;
    const r = await fetch(`/api/projects/${projectIdEnc}/contracts/${cid}/close`, { method: "POST" });
    if (r.ok) { tick(); toast.ok("Close submitted"); } else toast.error("Close failed");
  }
  async function closeAlpacaSymbol(symbol) {
    if (!await confirmModal(`Close Alpaca position ${symbol}? Submits a market order.`,
        { title: "Close position", okLabel: "Close" })) return;
    const r = await fetch(`/api/projects/${projectIdEnc}/alpaca_position/close`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol }),
    });
    if (r.ok) {
      tick();
    } else {
      const err = await r.json().catch(() => ({ detail: "unknown" }));
      toast.error("Close failed: " + (err.detail || r.statusText));
    }
  }

  // Parse OCC option symbol like "NVDA260608P00217500" into pretty parts.
  function parseOcc(sym) {
    const m = String(sym || "").match(/^([A-Z.]+)(\d{6})([CP])(\d{8})$/);
    if (!m) return null;
    const root = m[1];
    const y = "20" + m[2].slice(0, 2);
    const mo = m[2].slice(2, 4);
    const d = m[2].slice(4, 6);
    const right = m[3] === "C" ? "Call" : "Put";
    const strike = parseInt(m[4], 10) / 1000;
    return { root, expiration: `${y}-${mo}-${d}`, right, strike };
  }

  function renderContracts(snap) {
    const tbody = document.querySelector("#contracts-table tbody");
    const dbContracts = snap.contracts || [];
    const liveOpts = (snap.positions_live || []).filter(p => p.asset_class === "us_option");

    // Build a unified row list, indexed by option_symbol.
    // DB-tracked rows carry contract_id and strategy_phase; live-only rows
    // get filled from the OCC symbol.
    const dbBySym = {};
    dbContracts.forEach(c => { if (c.option_symbol) dbBySym[c.option_symbol] = c; });
    const liveBySym = {};
    liveOpts.forEach(p => { liveBySym[p.symbol] = p; });

    const allSymbols = new Set([
      ...Object.keys(dbBySym),
      ...Object.keys(liveBySym),
    ]);

    if (!allSymbols.size) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty">No open contracts</td></tr>';
      return;
    }

    const rows = [];
    allSymbols.forEach(sym => {
      const c = dbBySym[sym];
      const lp = liveBySym[sym];
      const occ = parseOcc(sym) || {};
      const ticker = (c && c.ticker) || occ.root || sym;
      const phase = (c && c.strategy_phase)
        || (occ.right === "Put" ? "CSP" : occ.right === "Call" ? "CC" : "—");
      const strike = (c && c.strike_price) || occ.strike;
      const premium = c ? c.premium_collected
        : (lp ? Math.abs(lp.avg_entry_price * 100) : null);
      const expiration = (c && c.expiration_date) || occ.expiration || "—";
      const tracked = !!c;
      const closeBtn = tracked
        ? `<button class="btn small danger" data-close-contract="${c.contract_id}" data-label="${escapeHtml(ticker + ' ' + phase)}">Close</button>`
        : `<button class="btn small danger" data-close-symbol="${escapeHtml(sym)}" title="Untracked — closes via Alpaca">Close*</button>`;
      rows.push({ ticker, sym, phase, strike, premium, expiration, tracked, closeBtn });
    });

    rows.sort((a, b) => a.ticker.localeCompare(b.ticker));

    tbody.innerHTML = rows.map(r => `
      <tr>
        <td><strong>${escapeHtml(r.ticker)}</strong>${r.tracked ? "" : ' <span class="muted" title="Open on Alpaca but not tracked in DB">∗</span>'}</td>
        <td><span class="badge ghost">${escapeHtml(r.phase)}</span></td>
        <td>${fmtMoney(r.strike)}</td>
        <td>${r.premium != null ? fmtMoney(r.premium) : "—"}</td>
        <td>${escapeHtml(String(r.expiration))}</td>
        <td>${r.closeBtn}</td>
      </tr>
    `).join("");

    tbody.querySelectorAll("[data-close-contract]").forEach(b =>
      b.addEventListener("click", () => closeContract(b.dataset.closeContract, b.dataset.label))
    );
    tbody.querySelectorAll("[data-close-symbol]").forEach(b =>
      b.addEventListener("click", () => closeAlpacaSymbol(b.dataset.closeSymbol))
    );
  }

  function renderTimeline(snap) {
    const host = document.getElementById("timeline");
    const tl = snap.timeline || [];
    if (!tl.length) {
      host.innerHTML = '<li class="empty">No activity yet.</li>';
      return;
    }
    host.innerHTML = tl.map(e => `
      <li class="event event-${e.kind}">
        <span class="event-icon">${e.icon}</span>
        <span class="event-message">${escapeHtml(e.message)}</span>
        <span class="event-time muted">${fmtTime(e.created_at)}</span>
      </li>
    `).join("");
  }

  function escapeHtml(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  async function loadEquityCurve() {
    const canvas = document.getElementById("dash-equity-curve");
    if (!canvas) return;
    try {
      const r = await fetch(`/api/projects/${projectIdEnc}/performance/equity_curve?period=month`);
      const pts = await r.json();
      const summary = document.getElementById("equity-summary");
      if (summary) summary.textContent = `${pts.length} snapshot(s)`;
      const ctx = canvas.getContext("2d");
      const w = canvas.clientWidth || canvas.width;
      const h = canvas.clientHeight || canvas.height;
      canvas.width = w; canvas.height = h;
      ctx.clearRect(0, 0, w, h);
      if (!pts.length) {
        ctx.fillStyle = "#9a948a"; ctx.font = "12px Inter, system-ui";
        ctx.textAlign = "center";
        ctx.fillText("No snapshots yet — they begin once cycles run.", w / 2, h / 2);
        return;
      }
      const values = pts.map(p => p.equity);
      const minV = Math.min(...values), maxV = Math.max(...values);
      const range = maxV - minV || 1;
      const pad = 24;
      ctx.strokeStyle = "#7e8fc1"; ctx.lineWidth = 2; ctx.beginPath();
      pts.forEach((p, i) => {
        const x = pad + ((w - pad - 6) * i) / Math.max(1, pts.length - 1);
        const y = (h - pad) - ((p.equity - minV) / range) * (h - pad - 6);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
      ctx.lineTo(w - 6, h - pad); ctx.lineTo(pad, h - pad);
      ctx.fillStyle = "rgba(126, 143, 193, 0.14)"; ctx.fill();
    } catch (e) { /* ignore */ }
  }

  async function tick() {
    try {
      const r = await fetch(`/api/projects/${projectIdEnc}/snapshot`, { cache: "no-store" });
      if (!r.ok) throw new Error(r.statusText);
      const snap = await r.json();
      renderBadges(snap);
      renderAccount(snap);
      renderWarnings(snap);
      renderPipeline(snap);
      renderChart(snap);
      renderPositions(snap);
      renderContracts(snap);
      renderTimeline(snap);
    } catch (e) {
      document.getElementById("last-update").textContent = "update failed: " + e.message;
    }
  }

  // Settings toggle + save
  const toggleBtn = document.getElementById("toggle-settings");
  const settingsForm = document.getElementById("project-settings-form");
  toggleBtn.addEventListener("click", () => {
    const open = settingsForm.style.display !== "none";
    settingsForm.style.display = open ? "none" : "block";
    toggleBtn.textContent = open ? "Show" : "Hide";
  });

  document.querySelectorAll('[data-action="save-project"]').forEach(btn => {
    btn.addEventListener("click", async (e) => {
      e.preventDefault();
      const row = btn.closest(".setting-row");
      const key = row.dataset.key;
      const type = row.dataset.type;
      const input = row.querySelector("[data-value]");
      let value = input.value;
      if (type === "int") value = parseInt(value, 10);
      else if (type === "float") value = parseFloat(value);
      else if (type === "bool") value = value === "true";
      const resp = await fetch(`/api/projects/${projectIdEnc}/settings`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, value, value_type: type }),
      });
      const original = btn.textContent;
      btn.textContent = resp.ok ? "Saved" : "Failed";
      btn.style.background = resp.ok ? "var(--ok)" : "var(--danger)";
      setTimeout(() => { btn.textContent = original; btn.style.background = ""; }, 1200);
    });
  });

  tick();
  loadEquityCurve();
  loadOverview();
  loadSparklines();
  setInterval(tick, 5000);
  setInterval(loadEquityCurve, 30000);
  setInterval(loadOverview, 30000);
  setInterval(loadSparklines, 60000);
})();
