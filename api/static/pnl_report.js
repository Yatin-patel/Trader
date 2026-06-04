// P&L Report — fetches /api/projects/{pid}/pnl_report and renders. The
// printable layout is in the HTML; we just fill it in.
(function () {
  const root = document.getElementById("pnl-root");
  if (!root) return;
  const projectId = root.dataset.projectId;
  const enc = encodeURIComponent(projectId);

  const ET_TZ = "America/New_York";
  const fmtMoney = (v) => {
    const n = typeof v === "number" ? v : parseFloat(v);
    if (isNaN(n)) return "—";
    const sign = n < 0 ? "-" : "";
    return sign + "$" + Math.abs(n).toLocaleString("en-US", {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
  };
  const fmtPct = (v) => {
    if (typeof v !== "number" || isNaN(v)) return "—";
    return (v * 100).toFixed(1) + "%";
  };
  const cls = (n) => (n > 0 ? "pl-pos" : n < 0 ? "pl-neg" : "");
  const _parseISO = (iso) => {
    if (!iso) return null;
    let s = String(iso);
    if (!/[Zz]|[+-]\d{2}:?\d{2}$/.test(s)) {
      s = s.replace(" ", "T") + "Z";
    }
    return new Date(s);
  };
  const fmtET = (iso) => {
    const d = _parseISO(iso);
    if (!d || isNaN(d.getTime())) return "—";
    return d.toLocaleString("en-US", {
      timeZone: ET_TZ, month: "short", day: "numeric", year: "numeric",
      hour: "2-digit", minute: "2-digit", hour12: false,
    }) + " ET";
  };
  const fmtDate = (iso) => {
    const d = _parseISO(iso);
    if (!d || isNaN(d.getTime())) return "—";
    return d.toLocaleDateString("en-US", {
      timeZone: ET_TZ, month: "short", day: "numeric", year: "numeric",
    });
  };

  function getRange() {
    const f = document.getElementById("pnl-from").value;
    const t = document.getElementById("pnl-to").value;
    return { from: f, to: t };
  }

  const fmtPctSigned = (v) => {
    if (typeof v !== "number" || isNaN(v)) return "—";
    const sign = v > 0 ? "+" : "";
    return sign + v.toFixed(2) + "%";
  };

  function applyReport(data) {
    const s = data.summary || {};
    const realEl = document.getElementById("card-realized");
    realEl.textContent = fmtMoney(s.realized_pnl);
    realEl.className = "card-value " + cls(s.realized_pnl);
    document.getElementById("card-realized-sub").textContent =
      `options ${fmtMoney(s.option_pnl)} · stock ${fmtMoney(s.stock_pnl)}`;

    // % gain on capital
    const pgEl = document.getElementById("card-pct-gain");
    const pg = s.pct_gain_on_capital;
    pgEl.textContent = fmtPctSigned(pg);
    pgEl.className = "card-value " + cls(pg);
    const pgSub = document.getElementById("card-pct-gain-sub");
    if (s.starting_equity) {
      const label = {
        snapshot: "vs equity at period start",
        earliest_snapshot: "vs first recorded equity",
        max_equity_allocation: "vs project budget",
      }[s.starting_source] || "vs capital";
      pgSub.textContent = `${fmtMoney(s.starting_equity)} ${label}`;
    } else {
      pgSub.textContent = "no capital reference";
    }

    // Annualized
    const annEl = document.getElementById("card-annualized");
    annEl.textContent = fmtPctSigned(s.annualized_pct);
    annEl.className = "card-value " + cls(s.annualized_pct);

    document.getElementById("card-premium").textContent = fmtMoney(s.total_premium);
    document.getElementById("card-winrate").textContent = fmtPct(s.win_rate);
    document.getElementById("card-trades").textContent =
      `${s.trade_count || 0} trades · ${s.wins || 0}W / ${s.losses || 0}L`;
    document.getElementById("card-pf").textContent =
      s.profit_factor == null ? (s.wins ? "∞" : "—") : s.profit_factor.toFixed(2);
    document.getElementById("card-drawdown").textContent =
      s.max_drawdown ? `max drawdown ${fmtMoney(s.max_drawdown)}` : "";

    const avgEl = document.getElementById("card-avg-wl");
    if (avgEl) {
      avgEl.textContent = `${fmtMoney(s.avg_winner)} / ${fmtMoney(s.avg_loser)}`;
    }
    const tcEl = document.getElementById("card-trade-count");
    if (tcEl) {
      tcEl.textContent = String(s.trade_count || 0);
    }
    const tsubEl = document.getElementById("card-trade-sub");
    if (tsubEl) {
      tsubEl.textContent = `${s.wins || 0} winners · ${s.losses || 0} losers`;
    }

    document.getElementById("pnl-report-range").textContent =
      `${fmtDate(data.from)} through ${fmtDate(data.to)}`;
    document.getElementById("pnl-report-generated").textContent =
      `Generated ${new Date().toLocaleString("en-US", { timeZone: ET_TZ })} ET`;

    // Monthly breakdown
    const monthlyBody = document.querySelector("#pnl-monthly tbody");
    if (!data.monthly || !data.monthly.length) {
      monthlyBody.innerHTML = '<tr><td colspan="7" class="empty">No closed trades in this date range.</td></tr>';
    } else {
      monthlyBody.innerHTML = data.monthly.map((m) => `<tr>
        <td>${m.month}</td>
        <td class="right ${cls(m.realized_pnl)}">${fmtMoney(m.realized_pnl)}</td>
        <td class="right">${fmtMoney(m.premium_captured)}</td>
        <td class="right">${m.trade_count}</td>
        <td class="right">${m.wins}</td>
        <td class="right">${m.losses}</td>
        <td class="right">${fmtPct(m.win_rate)}</td>
      </tr>`).join("");
    }

    // Closed trades
    const trades = data.closed_trades || [];
    document.getElementById("pnl-trades-count").textContent = `(${trades.length})`;
    const tradesBody = document.querySelector("#pnl-trades tbody");
    if (!trades.length) {
      tradesBody.innerHTML = '<tr><td colspan="10" class="empty">No closed trades in this date range.</td></tr>';
    } else {
      tradesBody.innerHTML = trades.map((c) => {
        const p = parseFloat(c.realized_pnl || 0);
        return `<tr>
          <td>${fmtET(c.closed_at)}</td>
          <td><code>${c.ticker || ""}</code></td>
          <td>${c.strategy_phase || ""}</td>
          <td class="right">${c.strike_price != null ? "$" + parseFloat(c.strike_price).toFixed(2) : "—"}</td>
          <td class="right">${c.quantity || ""}</td>
          <td class="right">${c.days_held != null ? c.days_held : "—"}</td>
          <td class="right">${fmtMoney(c.premium_collected)}</td>
          <td class="right">${fmtMoney(c.close_cost)}</td>
          <td class="right ${cls(p)}"><strong>${fmtMoney(p)}</strong></td>
          <td>${c.closure_reason || ""}</td>
        </tr>`;
      }).join("");
    }
  }

  async function load() {
    const { from, to } = getRange();
    const params = new URLSearchParams();
    if (from) params.set("from_", from);
    if (to) params.set("to", to);
    const csvLink = document.getElementById("pnl-csv-link");
    csvLink.href = `/api/projects/${enc}/pnl_report.csv?${params.toString()}`;
    try {
      const r = await fetch(`/api/projects/${enc}/pnl_report?${params.toString()}`);
      const out = await r.json();
      if (!r.ok) throw new Error(out.detail || r.statusText);
      applyReport(out);
    } catch (e) {
      console.error(e);
      if (window.toast && window.toast.error) window.toast.error("Failed to load: " + e.message);
    }
  }

  function setRange(from, to) {
    document.getElementById("pnl-from").value = from || "";
    document.getElementById("pnl-to").value = to || "";
    load();
  }

  function todayISO() {
    const d = new Date();
    return d.toISOString().slice(0, 10);
  }

  function isoNDaysAgo(n) {
    const d = new Date();
    d.setDate(d.getDate() - n);
    return d.toISOString().slice(0, 10);
  }

  // Initial range: prefer URL params, else this YTD.
  const initFrom = root.dataset.from
    || `${new Date().getFullYear()}-01-01`;
  const initTo = root.dataset.to || todayISO();
  document.getElementById("pnl-from").value = initFrom;
  document.getElementById("pnl-to").value = initTo;

  document.getElementById("pnl-refresh").addEventListener("click", load);
  document.getElementById("pnl-preset-month").addEventListener("click", () => {
    const d = new Date();
    const ym = d.toISOString().slice(0, 8);
    setRange(`${ym}01`, todayISO());
  });
  document.getElementById("pnl-preset-ytd").addEventListener("click",
    () => setRange(`${new Date().getFullYear()}-01-01`, todayISO()));
  document.getElementById("pnl-preset-12mo").addEventListener("click",
    () => setRange(isoNDaysAgo(365), todayISO()));
  document.getElementById("pnl-preset-all").addEventListener("click",
    () => setRange("1970-01-01", todayISO()));
  document.getElementById("pnl-print").addEventListener("click",
    () => window.print());

  load();
})();
