// Logs viewer — filters, search, expandable details, auto-refresh.

(function () {
  const root = document.getElementById("logs-root");
  const projectId = root.dataset.projectId;
  const enc = encodeURIComponent(projectId);

  const els = {
    node:    document.getElementById("f-node"),
    type:    document.getElementById("f-type"),
    search:  document.getElementById("f-search"),
    limit:   document.getElementById("f-limit"),
    refresh: document.getElementById("f-refresh"),
    clear:   document.getElementById("f-clear"),
    count:   document.getElementById("logs-count"),
    auto:    document.getElementById("logs-auto"),
    list:    document.getElementById("logs-list"),
    more:    document.getElementById("logs-load-more"),
  };

  let oldestId = null;     // for "load older"
  let lastQueryKey = "";   // detect filter changes
  let autoTimer = null;

  function buildUrl(opts = {}) {
    const p = new URLSearchParams();
    if (els.node.value)   p.set("node", els.node.value);
    if (els.type.value)   p.set("event_type", els.type.value);
    if (els.search.value) p.set("search", els.search.value);
    p.set("limit", els.limit.value);
    if (opts.before_id)   p.set("before_id", opts.before_id);
    return `/api/projects/${enc}/logs?${p.toString()}`;
  }

  function queryKey() {
    return [els.node.value, els.type.value, els.search.value, els.limit.value].join("|");
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function fmtTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString();
  }

  function renderEntry(e) {
    const li = document.createElement("li");
    li.className = `log-entry log-${e.kind || "info"}`;
    li.dataset.eventId = e.event_id;

    const headerRow = document.createElement("div");
    headerRow.className = "log-header";
    headerRow.innerHTML = `
      <span class="log-icon">${e.icon || "•"}</span>
      <span class="log-meta">
        <span class="badge ghost">${escapeHtml(e.node)}</span>
        <span class="badge ghost">${escapeHtml(e.event_type)}</span>
      </span>
      <span class="log-message">${escapeHtml((e.message || "").split("\n")[0])}</span>
      <span class="log-time muted">${fmtTime(e.created_at)}</span>
      <button class="log-toggle btn ghost small">Details</button>
    `;
    const detail = document.createElement("div");
    detail.className = "log-detail";
    detail.hidden = true;

    // Build the details body: full multi-line message + raw payload JSON.
    const multiline = (e.message || "").includes("\n") ? e.message : "";
    detail.innerHTML = `
      ${multiline ? `<pre class="log-narrative">${escapeHtml(multiline)}</pre>` : ""}
      <details class="log-raw">
        <summary class="muted">raw payload</summary>
        <pre>${escapeHtml(JSON.stringify(e.payload, null, 2))}</pre>
      </details>
    `;
    headerRow.querySelector(".log-toggle").addEventListener("click", () => {
      detail.hidden = !detail.hidden;
    });
    li.appendChild(headerRow);
    li.appendChild(detail);
    return li;
  }

  async function fetchAndRender(append = false) {
    const opts = append && oldestId ? { before_id: oldestId } : {};
    const url = buildUrl(opts);
    try {
      const resp = await fetch(url, { cache: "no-store" });
      if (!resp.ok) throw new Error(resp.statusText);
      const events = await resp.json();
      if (!append) els.list.innerHTML = "";
      if (events.length === 0 && !append) {
        els.list.innerHTML = '<li class="logs-empty">No events match these filters.</li>';
      }
      for (const e of events) els.list.appendChild(renderEntry(e));
      if (events.length > 0) {
        oldestId = events[events.length - 1].event_id;
      }
      els.count.textContent = `${els.list.querySelectorAll(".log-entry").length} shown`;
    } catch (err) {
      els.count.textContent = "fetch failed: " + err.message;
    }
  }

  function refresh() {
    const newKey = queryKey();
    const changed = newKey !== lastQueryKey;
    lastQueryKey = newKey;
    if (changed) oldestId = null;
    fetchAndRender(false);
  }

  function setupAuto() {
    if (autoTimer) clearInterval(autoTimer);
    if (els.auto.checked) {
      autoTimer = setInterval(() => fetchAndRender(false), 5000);
    }
  }

  els.refresh.addEventListener("click", refresh);
  els.clear.addEventListener("click", () => {
    els.node.value = ""; els.type.value = ""; els.search.value = ""; els.limit.value = "100";
    refresh();
  });
  [els.node, els.type, els.limit].forEach(el => el.addEventListener("change", refresh));
  els.search.addEventListener("keydown", (e) => { if (e.key === "Enter") refresh(); });
  els.auto.addEventListener("change", setupAuto);
  els.more.addEventListener("click", () => fetchAndRender(true));

  refresh();
  setupAuto();
})();
