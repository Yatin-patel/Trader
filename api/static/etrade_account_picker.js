// ETrade account picker — only mounts when the project page rendered
// the #etrade-account-picker section (broker_type=etrade AND OAuth
// tokens present).
(function () {
  const host = document.getElementById("etrade-account-picker");
  if (!host) return;
  const root = document.getElementById("project-root");
  if (!root) return;
  const projectId = root.dataset.projectId;
  const enc = encodeURIComponent(projectId);

  const sel = document.getElementById("etrade-account-select");
  const saveBtn = document.getElementById("etrade-save-account");
  const reloadBtn = document.getElementById("etrade-reload-accounts");
  const status = document.getElementById("etrade-status");
  const currentSpan = document.getElementById("etrade-current-key");

  const toastErr = (m) => {
    if (window.toast && window.toast.error) window.toast.error(m);
    else console.error("[etrade picker]", m);
  };
  const toastOk = (m) => {
    if (window.toast && window.toast.ok) window.toast.ok(m);
    else console.log("[etrade picker]", m);
  };

  async function loadAccounts() {
    sel.innerHTML = '<option value="">Loading…</option>';
    saveBtn.disabled = true;
    try {
      const r = await fetch(`/api/projects/${enc}/etrade/accounts`);
      const out = await r.json();
      if (!r.ok) {
        toastErr("Failed to load accounts: " + (out.detail || r.statusText));
        sel.innerHTML = '<option value="">(error)</option>';
        return;
      }
      const accounts = out.accounts || [];
      const currentKey = out.current_account_id_key || "";
      currentSpan.textContent = currentKey
        ? `currently: ${currentKey.slice(0, 20)}…`
        : "(none selected)";
      if (!accounts.length) {
        sel.innerHTML = '<option value="">(no accounts returned)</option>';
        return;
      }
      sel.innerHTML = accounts.map((a) => {
        const k = a.accountIdKey || "";
        const id = a.accountId || "?";
        const t = a.accountType || "—";
        const m = a.accountMode || "";
        const d = a.accountDesc || a.accountDescription || "";
        const label = `${id} · ${t}${m ? " · " + m : ""}${d ? " · " + d : ""}`;
        const selected = k === currentKey ? " selected" : "";
        return `<option value="${k}"${selected}>${label}</option>`;
      }).join("");
      saveBtn.disabled = false;
      status.textContent = `${accounts.length} account(s) available — pick one and click Save.`;
    } catch (e) {
      toastErr("Network error: " + e.message);
    }
  }

  async function saveAccount() {
    const key = sel.value;
    if (!key) {
      toastErr("Pick an account first.");
      return;
    }
    saveBtn.disabled = true;
    const orig = saveBtn.textContent;
    saveBtn.textContent = "Saving…";
    try {
      const r = await fetch(`/api/projects/${enc}/etrade/account`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id_key: key }),
      });
      const out = await r.json();
      if (!r.ok || out.error) {
        toastErr("Save failed: " + (out.detail || out.error || r.statusText));
      } else {
        toastOk("ETrade account selection saved.");
        currentSpan.textContent = `currently: ${key.slice(0, 20)}…`;
      }
    } catch (e) {
      toastErr("Network error: " + e.message);
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = orig;
    }
  }

  reloadBtn.addEventListener("click", loadAccounts);
  saveBtn.addEventListener("click", saveAccount);
  loadAccounts();
})();
