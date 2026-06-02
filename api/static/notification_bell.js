// Notification bell — polls unread count + most recent items.

(function () {
  console.log("[notification_bell.js v3] loaded");
  // Try to detect a project_id from the URL; fallback to first active project.
  let projectId = null;
  const m = location.pathname.match(/\/projects\/([^/?#]+)/);
  if (m) projectId = decodeURIComponent(m[1]);

  async function detectProject() {
    if (projectId) return projectId;
    try {
      const r = await fetch("/api/projects");
      if (!r.ok) throw new Error(`projects fetch ${r.status}`);
      const projs = await r.json();
      const active = projs.find(p => p.is_active) || projs[0];
      projectId = active?.project_id || null;
    } catch (e) {
      console.warn("bell: project detection failed", e);
    }
    return projectId;
  }

  const bell = document.createElement("div");
  bell.className = "bell-wrap";
  bell.innerHTML = `
    <button class="bell-btn" id="bell-btn" title="Notifications" aria-label="Notifications">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
           aria-hidden="true">
        <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
        <path d="M13.73 21a2 2 0 0 1-3.46 0"/>
      </svg>
      <span class="bell-count" id="bell-count" hidden>0</span>
    </button>
    <div class="bell-panel" id="bell-panel" hidden>
      <div class="bell-head">
        <strong>Notifications</strong>
        <button class="btn small ghost" id="bell-all-read">Mark all read</button>
      </div>
      <ul class="bell-list" id="bell-list">
        <li class="muted" style="padding: 12px;">Loading…</li>
      </ul>
      <div class="bell-foot">
        <a id="bell-link" href="#" class="muted">View all →</a>
      </div>
    </div>
  `;
  document.querySelector(".topbar")?.appendChild(bell);

  const btn = bell.querySelector("#bell-btn");
  const panel = bell.querySelector("#bell-panel");
  const count = bell.querySelector("#bell-count");
  const list = bell.querySelector("#bell-list");
  const link = bell.querySelector("#bell-link");
  const allRead = bell.querySelector("#bell-all-read");

  btn.addEventListener("click", async () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) loadList();
  });

  document.addEventListener("click", (e) => {
    if (!bell.contains(e.target)) panel.hidden = true;
  });

  allRead.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (!projectId) return;
    await fetch(`/api/projects/${encodeURIComponent(projectId)}/notifications/mark_read`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ all_unread: true }),
    });
    refresh();
    loadList();
  });

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  async function refresh() {
    if (!await detectProject()) {
      link.href = "/dashboard";
      return;
    }
    try {
      const r = await fetch(`/api/projects/${encodeURIComponent(projectId)}/notifications/unread_count`);
      if (!r.ok) throw new Error(`unread_count ${r.status}`);
      const d = await r.json();
      const n = d.unread || 0;
      count.textContent = n;
      count.hidden = n === 0;
      btn.classList.toggle("has-unread", n > 0);
      link.href = `/projects/${encodeURIComponent(projectId)}/notifications`;
    } catch (e) {
      console.warn("bell: unread_count failed", e);
    }
  }

  async function loadList() {
    console.log("[bell] loadList: starting");
    list.innerHTML = '<li class="muted" style="padding:12px;">Loading…</li>';
    const pid = await detectProject();
    console.log("[bell] loadList: pid =", pid);
    if (!pid) {
      list.innerHTML = '<li class="muted" style="padding:12px;">No projects yet. Add one from the dashboard.</li>';
      return;
    }
    // Hard 8-second timeout so the dropdown never sticks on "Loading…".
    const ctrl = new AbortController();
    const killTimer = setTimeout(() => ctrl.abort(), 8000);
    try {
      const url = `/api/projects/${encodeURIComponent(pid)}/notifications?limit=10`;
      console.log("[bell] loadList: fetching", url);
      const r = await fetch(url, { signal: ctrl.signal, cache: "no-store" });
      console.log("[bell] loadList: response status =", r.status);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const items = await r.json();
      console.log("[bell] loadList: items count =", Array.isArray(items) ? items.length : "(not array)");
      if (!Array.isArray(items) || items.length === 0) {
        list.innerHTML = '<li class="muted" style="padding:12px;">No notifications yet.</li>';
        return;
      }
      const html = items.map(n => {
        const sevClass = ["error", "critical", "warn"].includes(n.severity) ? "bell-bad" : "";
        const t = n.created_at ? new Date(n.created_at).toLocaleString() : "";
        return `<li class="bell-item ${n.read_at ? '' : 'unread'} ${sevClass}">
          <div class="bell-title">${escapeHtml(n.title)}</div>
          <div class="bell-meta muted">${escapeHtml(n.event_type || "")} · ${escapeHtml(t)}</div>
        </li>`;
      }).join("");
      list.innerHTML = html;
      console.log("[bell] loadList: DOM updated");
    } catch (e) {
      console.error("[bell] loadList failed", e);
      const msg = e.name === "AbortError" ? "timed out after 8s" : e.message;
      list.innerHTML = `<li class="muted bell-bad" style="padding:12px;">Failed to load: ${escapeHtml(msg)}</li>`;
    } finally {
      clearTimeout(killTimer);
    }
  }

  window.__notif_refresh_bell = refresh;
  refresh();
  // Pre-load list on init so the dropdown is fresh the moment it opens.
  loadList();
  setInterval(() => { refresh(); loadList(); }, 15000);
})();
