// Settings Backup & Migration — multi-project version on /account.
// Pickers let the user pick the source/target project for each action.
(function () {
  const host = document.getElementById("settings-migration");
  if (!host) return;
  // toast.js loads AFTER content scripts in _layout.html, so capture
  // window.toast lazily inside each handler — never at module-load
  // time. Without this we'd fall back to ugly native alerts.
  const _toast = (kind) => (m) => {
    const t = window.toast;
    if (t && t[kind]) t[kind](m);
    else console[kind === "error" ? "error" : "log"]("[settings] " + m);
  };
  const toast = { ok: _toast("ok"), error: _toast("error"), info: _toast("info") };
  const _confirm = (msg, opts) => {
    if (window.confirmModal) return window.confirmModal(msg, opts);
    return Promise.resolve(window.confirm(msg));
  };

  function summary(r) {
    const a = (r.applied || []).length;
    const s = (r.skipped || []).length;
    const e = (r.errors || []).length;
    let msg = `Applied ${a} setting${a === 1 ? "" : "s"}`;
    if (s) msg += `, skipped ${s}`;
    if (e) {
      msg += `, ${e} error${e === 1 ? "" : "s"}`;
      console.warn("settings migration errors", r.errors);
    }
    return msg;
  }
  const enc = (s) => encodeURIComponent(s);

  // ----- Export: keep the download link in sync with the picker --------
  const exportSel = document.getElementById("sm-export-project");
  const exportLink = document.getElementById("sm-export-link");
  function syncExportLink() {
    if (!exportSel || !exportLink) return;
    const pid = exportSel.value;
    if (pid) {
      exportLink.href = `/api/projects/${enc(pid)}/settings/export`;
      exportLink.removeAttribute("aria-disabled");
    } else {
      exportLink.href = "#";
    }
  }
  if (exportSel) {
    exportSel.addEventListener("change", syncExportLink);
    syncExportLink();
  }

  // ----- Import file ----------------------------------------------------
  const importBtn = document.getElementById("sm-import-btn");
  if (importBtn) {
    importBtn.addEventListener("click", async () => {
      const pid = document.getElementById("sm-import-project").value;
      const fileInput = document.getElementById("sm-import-file");
      const file = fileInput && fileInput.files && fileInput.files[0];
      if (!pid) { toast.error("Pick a target project."); return; }
      if (!file) { toast.error("Pick a JSON file first."); return; }
      const overwrite = document.getElementById("sm-import-overwrite").checked;
      const fd = new FormData();
      fd.append("file", file);
      importBtn.disabled = true;
      const orig = importBtn.textContent;
      importBtn.textContent = "Importing…";
      try {
        const r = await fetch(
          `/api/projects/${enc(pid)}/settings/import?overwrite=${overwrite}`,
          { method: "POST", body: fd },
        );
        const out = await r.json();
        if (!r.ok || out.error) {
          toast.error("Import failed: " + (out.detail || out.error || r.statusText));
        } else {
          toast.ok(summary(out));
        }
      } catch (e) {
        toast.error("Network error: " + e.message);
      } finally {
        importBtn.disabled = false;
        importBtn.textContent = orig;
      }
    });
  }

  // ----- Clone source -> destination ------------------------------------
  const cloneBtn = document.getElementById("sm-clone-btn");
  if (cloneBtn) {
    cloneBtn.addEventListener("click", async () => {
      const src = document.getElementById("sm-clone-source").value;
      const dst = document.getElementById("sm-clone-dest").value;
      if (!src || !dst) { toast.error("Pick both source and destination."); return; }
      if (src === dst) { toast.error("Source and destination must be different projects."); return; }
      const overwrite = document.getElementById("sm-clone-overwrite").checked;
      const proceed = await _confirm(
        `Copy ALL settings from "${src}" into "${dst}"? This will ${overwrite ? "OVERWRITE existing values" : "only fill in keys that are not set"}.`,
        { title: "Clone settings", okLabel: "Clone", danger: false }
      );
      if (!proceed) return;
      cloneBtn.disabled = true;
      const orig = cloneBtn.textContent;
      cloneBtn.textContent = "Cloning…";
      try {
        const r = await fetch(`/api/projects/${enc(dst)}/settings/clone_from`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source_project_id: src, overwrite }),
        });
        const out = await r.json();
        if (!r.ok || out.error) {
          toast.error("Clone failed: " + (out.detail || out.error || r.statusText));
        } else {
          toast.ok(summary(out));
        }
      } catch (e) {
        toast.error("Network error: " + e.message);
      } finally {
        cloneBtn.disabled = false;
        cloneBtn.textContent = orig;
      }
    });
  }
})();
