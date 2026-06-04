// Tax Report page — fetches capital_gains_summary for the selected year
// and renders the cards + by-ticker table. The Form 8949 detail rows are
// served as a CSV download (anchor href), not rendered in the page.
(function () {
  const root = document.getElementById("tax-root");
  if (!root) return;
  const projectId = root.dataset.projectId;
  const enc = encodeURIComponent(projectId);

  const fmtMoney = (v) => {
    const n = typeof v === "number" ? v : parseFloat(v);
    if (isNaN(n)) return "—";
    const sign = n < 0 ? "-" : "";
    const abs = Math.abs(n).toLocaleString("en-US", {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
    return sign + "$" + abs;
  };
  const cls = (n) => (n > 0 ? "pl-pos" : n < 0 ? "pl-neg" : "");

  function setCard(id, v) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = fmtMoney(v);
    el.className = "card-value " + cls(v);
  }

  function applySummary(summary) {
    const st = summary.short_term_total || 0;
    const lt = summary.long_term_total || 0;
    const total = summary.grand_total || 0;
    const byT = summary.by_ticker || [];
    const bd = summary.breakdown || {};

    setCard("card-st", st);
    setCard("card-lt", lt);
    setCard("card-total", total);
    document.getElementById("card-total-year").textContent = "Tax year " + summary.year;
    document.getElementById("card-tickers").textContent = String(byT.length);

    setCard("card-opt-st", bd.option_short_term || 0);
    setCard("card-opt-lt", bd.option_long_term || 0);
    setCard("card-stk-st", bd.stock_short_term || 0);
    setCard("card-stk-lt", bd.stock_long_term || 0);
    const stSub = document.getElementById("card-st-sub");
    if (stSub) {
      stSub.textContent = `options ${fmtMoney(bd.option_short_term || 0)} · stock ${fmtMoney(bd.stock_short_term || 0)}`;
    }
    const ltSub = document.getElementById("card-lt-sub");
    if (ltSub) {
      ltSub.textContent = `options ${fmtMoney(bd.option_long_term || 0)} · stock ${fmtMoney(bd.stock_long_term || 0)}`;
    }

    const tbody = document.querySelector("#tax-by-ticker tbody");
    if (!byT.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="empty">No closed lots for this year yet.</td></tr>';
    } else {
      tbody.innerHTML = byT.map((r) => {
        const sv = r.short || 0, lv = r.long || 0, tot = sv + lv;
        return `<tr>
          <td><code>${r.ticker}</code></td>
          <td class="right ${cls(sv)}">${fmtMoney(sv)}</td>
          <td class="right ${cls(lv)}">${fmtMoney(lv)}</td>
          <td class="right ${cls(tot)}"><strong>${fmtMoney(tot)}</strong></td>
        </tr>`;
      }).join("");
    }
    const setFoot = (id, v) => {
      const el = document.getElementById(id);
      el.textContent = fmtMoney(v);
      el.className = "right " + cls(v);
    };
    setFoot("foot-st", st);
    setFoot("foot-lt", lt);
    setFoot("foot-total", total);
  }

  function _err(msg) {
    if (window.toast && window.toast.error) window.toast.error(msg);
    else console.error("[tax_report]", msg);
  }

  async function load(year) {
    try {
      const r = await fetch(`/api/projects/${enc}/tax_lots/capital_gains?year=${year}`);
      const out = await r.json();
      applySummary(out);
    } catch (e) {
      console.error(e);
      _err("Failed to load tax summary: " + e.message);
    }
  }

  const sel = document.getElementById("tax-year");
  sel.addEventListener("change", () => {
    const y = sel.value;
    // Update the CSV link to the new year too.
    const link = document.getElementById("tax-csv-link");
    link.href = `/api/projects/${enc}/tax_report/form_8949.csv?year=${y}`;
    // Update the URL bar so the page is shareable / refresh-safe.
    const url = new URL(window.location.href);
    url.searchParams.set("year", y);
    window.history.replaceState({}, "", url.toString());
    load(y);
  });

  load(parseInt(root.dataset.selectedYear, 10));
})();
