(function () {
  const fmtMoney = v => v == null ? "—" : "$" + Number(v).toFixed(4);
  const fmt = v => v == null ? "—" : Number(v).toLocaleString();
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function load() {
    try {
      const usage = await (await fetch("/api/llm/usage?limit=50")).json();
      const cache = await (await fetch("/api/llm/cache_stats")).json();
      document.getElementById("c-calls").textContent = usage.summary.today.calls;
      document.getElementById("c-tokens").textContent = fmt(usage.summary.today.tokens);
      document.getElementById("c-cost").textContent = fmtMoney(usage.summary.today.cost_usd);
      document.getElementById("c-hit").textContent = (cache.hit_rate * 100).toFixed(1) + "%";

      const modelTbody = document.querySelector("#model-table tbody");
      const by = usage.summary.by_model_30d || [];
      modelTbody.innerHTML = by.length
        ? by.map(m => `<tr><td><code>${esc(m.model)}</code></td><td>${m.calls}</td><td>${fmt(m.tokens)}</td><td>${fmtMoney(m.cost_usd)}</td></tr>`).join("")
        : '<tr><td colspan="4" class="empty">No calls in last 30 days.</td></tr>';

      const usageTbody = document.querySelector("#usage-table tbody");
      const rows = usage.recent || [];
      usageTbody.innerHTML = rows.length
        ? rows.map(u => `<tr>
            <td>${new Date(u.created_at).toLocaleString()}</td>
            <td>${esc(u.purpose)}</td>
            <td><code style="font-size:11px">${esc(u.model)}</code></td>
            <td>${fmt(u.prompt_tokens)}</td>
            <td>${fmt(u.completion_tokens)}</td>
            <td>${fmt(u.total_tokens)}</td>
            <td>${fmtMoney(u.cost_usd)}</td>
            <td>${u.cache_hit ? "✓" : "—"}</td>
          </tr>`).join("")
        : '<tr><td colspan="8" class="empty">No calls tracked yet.</td></tr>';
    } catch (e) {
      console.error(e);
    }
  }
  document.getElementById("cost-refresh").addEventListener("click", load);
  load();
  setInterval(load, 15000);
})();
