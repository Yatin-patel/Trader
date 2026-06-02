// Global toaster — replaces alert() calls.
// Usage:  toast.show("Saved", "ok")  |  toast.show("err", "error")
//         toast.ok(msg)  toast.error(msg)  toast.info(msg)  toast.warn(msg)
// Toasts stack and auto-dismiss after ~4s; click to dismiss early.

(function () {
  let container = document.getElementById("toast-container");
  if (!container) {
    container = document.createElement("div");
    container.id = "toast-container";
    document.body.appendChild(container);
  }

  function show(message, kind = "info", ttlMs = 4500) {
    const t = document.createElement("div");
    t.className = "toast toast-" + kind;
    t.textContent = String(message);
    t.addEventListener("click", () => dismiss(t));
    container.appendChild(t);
    // Force reflow then add visible class for the fade-in transition.
    void t.offsetWidth;
    t.classList.add("toast-show");
    setTimeout(() => dismiss(t), ttlMs);
    return t;
  }

  function dismiss(t) {
    if (!t || t._dismissed) return;
    t._dismissed = true;
    t.classList.remove("toast-show");
    setTimeout(() => t.remove(), 250);
  }

  window.toast = {
    show,
    ok:    (m) => show(m, "ok"),
    error: (m) => show(m, "error"),
    warn:  (m) => show(m, "warn"),
    info:  (m) => show(m, "info"),
  };

  // ---------- Custom confirmation dialog (replaces native confirm) ----------
  // Returns a Promise<boolean>. Styled to match the rest of the app.
  function ensureConfirmDialog() {
    let dlg = document.getElementById("__confirm-dialog");
    if (dlg) return dlg;
    dlg = document.createElement("dialog");
    dlg.id = "__confirm-dialog";
    dlg.innerHTML = `
      <form method="dialog" style="min-width: 360px; max-width: 480px;">
        <h2 id="__confirm-title" style="margin: 0 0 8px; font-size: 17px;">Confirm</h2>
        <p id="__confirm-message" style="margin: 0 0 18px; color: var(--text-soft); line-height: 1.45;"></p>
        <div class="dialog-actions">
          <button type="button" class="btn ghost small" data-confirm-cancel>Cancel</button>
          <button type="button" class="btn danger small" data-confirm-ok>Confirm</button>
        </div>
      </form>`;
    document.body.appendChild(dlg);
    return dlg;
  }

  function confirmModal(message, opts = {}) {
    return new Promise((resolve) => {
      const dlg = ensureConfirmDialog();
      const titleEl = dlg.querySelector("#__confirm-title");
      const msgEl = dlg.querySelector("#__confirm-message");
      const okBtn = dlg.querySelector("[data-confirm-ok]");
      const cancelBtn = dlg.querySelector("[data-confirm-cancel]");

      titleEl.textContent = opts.title || "Confirm";
      msgEl.textContent = message;
      okBtn.textContent = opts.okLabel || "Confirm";
      cancelBtn.textContent = opts.cancelLabel || "Cancel";
      okBtn.className = "btn small " + (opts.danger === false ? "primary" : "danger");

      function cleanup(result) {
        okBtn.removeEventListener("click", onOk);
        cancelBtn.removeEventListener("click", onCancel);
        dlg.removeEventListener("close", onClose);
        dlg.close();
        resolve(result);
      }
      function onOk()     { cleanup(true); }
      function onCancel() { cleanup(false); }
      function onClose()  { cleanup(false); }
      okBtn.addEventListener("click", onOk);
      cancelBtn.addEventListener("click", onCancel);
      dlg.addEventListener("close", onClose);

      dlg.showModal();
    });
  }
  window.confirmModal = confirmModal;
})();
