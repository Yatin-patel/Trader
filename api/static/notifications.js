(function () {
  console.log("[notifications.js v3] loaded");
  const root = document.getElementById("notif-root");
  if (!root) {
    console.error("[notifications.js] no #notif-root element — wrong page?");
    return;
  }
  const projectId = root.dataset.projectId;
  console.log("[notifications.js] projectId =", projectId);
  const enc = encodeURIComponent(projectId);
  const dialog = document.getElementById("channel-dialog");
  const form = document.getElementById("channel-form");

  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtTime = iso => iso ? new Date(iso).toLocaleString() : "—";

  async function loadChannels() {
    const tbody = document.querySelector("#channels-table tbody");
    tbody.innerHTML = '<tr><td colspan="8" class="empty">Loading channels…</td></tr>';
    try {
      const r = await fetch(`/api/projects/${enc}/notifications/channels`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const rows = await r.json();
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty">No channels yet — click "+ Add channel" to set one up.</td></tr>';
        return;
      }
      tbody.innerHTML = rows.map(c => `
        <tr>
          <td><span class="badge ghost">${esc(c.channel_type)}</span></td>
          <td>${esc(c.name)}</td>
          <td><code>${esc(c.target_masked || c.target || "")}</code></td>
          <td>${esc((c.events_filter || []).join(", ") || "(defaults)")}</td>
          <td>${c.enabled ? "✓" : "—"}</td>
          <td>${c.send_count || 0}</td>
          <td class="${c.last_error ? 'pl-neg' : ''}">${esc(c.last_error || "—")}</td>
          <td>
            <button class="btn small ghost" data-test='${c.channel_id}'>Test</button>
            <button class="btn small ghost" data-edit='${JSON.stringify(c).replace(/'/g, '&#39;')}'>Edit</button>
            <button class="btn small danger" data-del='${c.channel_id}'>Delete</button>
          </td>
        </tr>`).join("");
      tbody.querySelectorAll("[data-test]").forEach(b => b.addEventListener("click", () => testChannel(b.dataset.test)));
      tbody.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", () => deleteChannel(b.dataset.del)));
      tbody.querySelectorAll("[data-edit]").forEach(b => b.addEventListener("click", () => openDialog(JSON.parse(b.dataset.edit))));
    } catch (e) {
      console.error("loadChannels failed", e);
      tbody.innerHTML = `<tr><td colspan="8" class="empty pl-neg">Error loading channels: ${esc(e.message)}</td></tr>`;
    }
  }

  async function loadHistory() {
    const tbody = document.querySelector("#notif-history tbody");
    try {
      const r = await fetch(`/api/projects/${enc}/notifications?limit=50`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const rows = await r.json();
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty">No notifications yet.</td></tr>';
        return;
      }
      tbody.innerHTML = rows.map(n => {
        const sevClass = { critical: "pl-neg", error: "pl-neg",
                           warn: "pl-neg", info: "" }[n.severity] || "";
        return `<tr>
          <td>${fmtTime(n.created_at)}</td>
          <td class="${sevClass}">${esc(n.severity)}</td>
          <td><code>${esc(n.event_type || "")}</code></td>
          <td>${esc(n.title)}</td>
          <td>${esc(n.status)}</td>
          <td>${n.read_at ? "✓" : "—"}</td>
        </tr>`;
      }).join("");
    } catch (e) {
      console.error("loadHistory failed", e);
      tbody.innerHTML = `<tr><td colspan="6" class="empty pl-neg">Error loading history: ${esc(e.message)}</td></tr>`;
    }
  }

  function openDialog(existing) {
    document.getElementById("channel-form-title").textContent =
      existing ? "Edit Channel" : "New Channel";
    form.reset();
    form.elements["channel_id"].value = existing?.channel_id ?? "";
    form.elements["channel_type"].value = existing?.channel_type ?? "discord";
    form.elements["name"].value = existing?.name ?? "";
    form.elements["target"].value = existing?.target ?? "";
    form.elements["events_filter"].value = (existing?.events_filter || []).join(",");
    form.elements["enabled"].checked = existing ? existing.enabled : true;
    const cfg = existing?.config || {};
    ["smtp_host","smtp_port","smtp_user","smtp_password","from"].forEach(k => {
      if (form.elements[k]) form.elements[k].value = cfg[k] ?? (k === "smtp_port" ? 587 : "");
    });
    form.elements["use_tls"].checked = cfg.use_tls !== false;
    toggleEmailFields();
    dialog.showModal();
  }

  function toggleEmailFields() {
    document.getElementById("email-fields").hidden =
      form.elements["channel_type"].value !== "email";
  }

  form.elements["channel_type"].addEventListener("change", toggleEmailFields);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const payload = {
      channel_type: fd.get("channel_type"),
      name: fd.get("name"),
      target: fd.get("target"),
      enabled: fd.get("enabled") === "on",
    };
    const eventsStr = (fd.get("events_filter") || "").toString().trim();
    if (eventsStr) {
      payload.events_filter = eventsStr.split(",").map(s => s.trim()).filter(Boolean);
    }
    if (payload.channel_type === "email") {
      payload.config = {
        smtp_host: fd.get("smtp_host"),
        smtp_port: parseInt(fd.get("smtp_port") || "587", 10),
        smtp_user: fd.get("smtp_user"),
        smtp_password: fd.get("smtp_password"),
        from: fd.get("from"),
        use_tls: fd.get("use_tls") === "on",
      };
    }
    const cid = fd.get("channel_id");
    if (cid) payload.channel_id = parseInt(cid, 10);
    const r = await fetch(`/api/projects/${enc}/notifications/channels`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (r.ok) { dialog.close(); loadChannels(); toast.ok("Channel saved"); }
    else { toast.error("Save failed: " + (await r.text())); }
  });

  document.getElementById("add-channel").addEventListener("click", () => openDialog(null));
  dialog.querySelectorAll("[data-close]").forEach(b =>
    b.addEventListener("click", () => dialog.close()));

  document.getElementById("digest-now").addEventListener("click", async () => {
    const r = await fetch(`/api/projects/${enc}/notifications/digest_now`, { method: "POST" });
    const out = await r.json();
    toast.ok("Dispatched to " + (out.results || []).length + " channel(s)");
    loadHistory();
  });

  document.getElementById("mark-all-read").addEventListener("click", async () => {
    await fetch(`/api/projects/${enc}/notifications/mark_read`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ all_unread: true }),
    });
    loadHistory();
    if (window.__notif_refresh_bell) window.__notif_refresh_bell();
  });

  async function testChannel(cid) {
    const r = await fetch(`/api/projects/${enc}/notifications/test`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel_id: parseInt(cid, 10) }),
    });
    const out = await r.json();
    if (out.ok) toast.ok("Test sent ✓");
    else toast.error("Failed: " + (out.error || "?"));
    loadChannels();
    loadHistory();
  }

  async function deleteChannel(cid) {
    if (!await confirmModal("Delete this notification channel?",
        { title: "Delete channel", okLabel: "Delete" })) return;
    await fetch(`/api/projects/${enc}/notifications/channels/${cid}`, { method: "DELETE" });
    loadChannels();
    toast.ok("Channel deleted");
  }

  loadChannels();
  loadHistory();
  setInterval(loadHistory, 15000);
})();
