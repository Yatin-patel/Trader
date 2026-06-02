// Optimize button — auto-previews on open and whenever the strategy
// dropdown changes. Apply button is always enabled and applies the
// currently-selected strategy.

(function () {
  const dialog = document.getElementById("optimize-dialog");
  const openBtn = document.getElementById("open-optimize");
  if (!dialog || !openBtn) return;

  // Read project id from the sub-nav (lives in _layout.html, so works on
  // every project-scoped page, not just the dashboard).
  const subnav = document.querySelector(".project-subnav");
  const projectId = subnav && subnav.dataset.projectId;
  if (!projectId) return;
  const enc = encodeURIComponent(projectId);

  const previewBtn = document.getElementById("optimize-preview-btn");
  const applyBtn = document.getElementById("optimize-apply-btn");
  const cancelBtn = dialog.querySelector('button[value="cancel"]');
  const strategySel = document.getElementById("optimize-strategy");
  const previewBox = document.getElementById("optimize-preview");

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // Apply is always enabled — no need to preview first.
  applyBtn.disabled = false;
  // Preview button label clarified.
  if (previewBtn) previewBtn.textContent = "Refresh preview";

  openBtn.addEventListener("click", async () => {
    dialog.showModal();
    await runPreview();   // auto-load when opened
  });
  cancelBtn.addEventListener("click", () => dialog.close());

  // Re-preview whenever strategy changes.
  strategySel.addEventListener("change", runPreview);

  // Manual refresh.
  if (previewBtn) previewBtn.addEventListener("click", runPreview);

  async function runPreview() {
    const strategy = strategySel.value;
    previewBox.innerHTML = '<span class="muted">Checking your Alpaca account…</span>';
    try {
      const r = await fetch(
        `/api/projects/${enc}/optimize/preview?strategy=${encodeURIComponent(strategy)}`,
        { cache: "no-store" }
      );
      const data = await r.json();
      if (!r.ok) {
        previewBox.innerHTML = `<span class="pl-neg">Preview failed: ${esc(data.detail || "unknown")}</span>`;
        return;
      }
      renderPreview(data);
    } catch (e) {
      previewBox.innerHTML = `<span class="pl-neg">Network error: ${esc(e.message)}</span>`;
    }
  }

  const iterateChk = document.getElementById("optimize-iterate");
  const iterLog = document.getElementById("optimize-iter-log");

  function iterAppend(html) {
    iterLog.hidden = false;
    iterLog.insertAdjacentHTML("beforeend", html);
    iterLog.scrollTop = iterLog.scrollHeight;
  }

  async function runAIIterations(maxRounds = 5) {
    iterAppend(`<div class="muted">Starting AI iterations (up to ${maxRounds} rounds)…</div>`);
    for (let i = 1; i <= maxRounds; i++) {
      iterAppend(`<div style="margin-top:8px;"><strong>Round ${i}/${maxRounds}</strong> · asking AI for suggestions…</div>`);
      let rec;
      try {
        const r = await fetch(`/api/projects/${enc}/recommendations/build`, { method: "POST" });
        rec = await r.json();
        if (!r.ok || rec.error) {
          iterAppend(`<div class="pl-neg">  AI call failed: ${esc((rec && rec.error) || ("HTTP " + r.status))}</div>`);
          return { stopped: "ai_error", rounds: i };
        }
      } catch (e) {
        iterAppend(`<div class="pl-neg">  Network error: ${esc(e.message)}</div>`);
        return { stopped: "network", rounds: i };
      }
      const changes = rec.changes || {};
      const changeKeys = Object.keys(changes);
      if (changeKeys.length === 0) {
        iterAppend(`<div class="pl-pos">  ✓ AI is satisfied — no more changes recommended.</div>`);
        return { stopped: "satisfied", rounds: i };
      }
      iterAppend(`<div>  AI suggests: <code>${esc(JSON.stringify(changes))}</code></div>`);
      // Apply
      try {
        const ar = await fetch(`/api/projects/${enc}/recommendations/${rec.rec_id}/apply`, { method: "POST" });
        const adata = await ar.json();
        if (!ar.ok || adata.error) {
          iterAppend(`<div class="pl-neg">  Apply failed: ${esc((adata && adata.error) || ("HTTP " + ar.status))}</div>`);
          return { stopped: "apply_error", rounds: i };
        }
        const appliedKeys = Object.keys(adata.applied || {});
        iterAppend(`<div class="pl-pos">  ✓ Applied: ${esc(appliedKeys.join(", "))}</div>`);
      } catch (e) {
        iterAppend(`<div class="pl-neg">  Network error on apply: ${esc(e.message)}</div>`);
        return { stopped: "network", rounds: i };
      }
    }
    iterAppend(`<div class="muted">Reached max ${maxRounds} rounds.</div>`);
    return { stopped: "max_rounds", rounds: maxRounds };
  }

  applyBtn.addEventListener("click", async () => {
    const strategy = strategySel.value;
    const stratLabel = strategySel.options[strategySel.selectedIndex].text;
    const willIterate = iterateChk && iterateChk.checked;

    const promptMsg = willIterate
      ? `Apply "${stratLabel}" settings, then iterate AI suggestions up to 5 rounds?`
      : `Apply "${stratLabel}" settings? This overrides your current project settings.`;
    if (!await confirmModal(promptMsg,
        { title: "Apply optimization", okLabel: "Apply", danger: false })) {
      return;
    }

    applyBtn.disabled = true;
    applyBtn.textContent = "Applying…";
    iterLog.innerHTML = "";
    iterLog.hidden = true;
    try {
      // Step 1: apply the cash-tier-aware template
      const r = await fetch(`/api/projects/${enc}/optimize`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ strategy }),
      });
      const data = await r.json();
      if (!r.ok) {
        toast.error("Apply failed: " + (data.detail || "unknown"));
        return;
      }
      const n = Object.keys(data.applied || {}).length;
      toast.ok(`Applied ${n} settings (${data.tier} tier · ${data.strategy})`);

      // Step 2 (optional): iterate AI recommendations
      if (willIterate) {
        applyBtn.textContent = "Iterating…";
        const result = await runAIIterations(5);
        const msg = ({
          satisfied: `AI converged after ${result.rounds} round(s).`,
          max_rounds: `Stopped at max ${result.rounds} rounds.`,
          ai_error: `AI errored after ${result.rounds} round(s).`,
          apply_error: `Apply errored after ${result.rounds} round(s).`,
          network: `Network error after ${result.rounds} round(s).`,
        })[result.stopped] || `Done after ${result.rounds} round(s).`;
        if (result.stopped === "satisfied") toast.ok(msg);
        else if (result.stopped === "max_rounds") toast.info(msg);
        else toast.warn(msg);
        // Give the user a moment to read the log before reload.
        setTimeout(() => {
          dialog.close();
          window.location.reload();
        }, 2500);
        return;
      }

      // Non-iterating path: close + reload as before.
      dialog.close();
      setTimeout(() => window.location.reload(), 800);
    } catch (e) {
      toast.error("Network error: " + e.message);
    } finally {
      applyBtn.disabled = false;
      applyBtn.textContent = "Apply";
    }
  });

  function renderPreview(d) {
    const s = d.settings || {};
    const notesHtml = (d.notes || []).map(n => `<div>• ${esc(n)}</div>`).join("");
    const keyRows = [
      "max_concentration_per_ticker", "max_collateral_pct",
      "contracts_per_csp", "watchlist",
      "csp_delta_min", "csp_delta_max", "csp_min_dte", "csp_max_dte",
      "min_iv_rank", "scanner_min_price", "scanner_max_price",
    ].filter(k => k in s).map(k => {
      let v = s[k];
      if (typeof v === "string" && v.length > 60) v = v.slice(0, 60) + "…";
      return `<tr><td><code>${esc(k)}</code></td><td><strong>${esc(String(v))}</strong></td></tr>`;
    }).join("");

    previewBox.innerHTML = `
      <div style="margin-bottom: 8px;">
        <strong>${esc(d.strategy)}</strong>
        · cash <strong>$${(d.cash || 0).toLocaleString()}</strong>
        · BP <strong>$${(d.buying_power || 0).toLocaleString()}</strong>
        · tier <strong>${esc(d.tier)}</strong>
      </div>
      <div style="margin-bottom: 10px; line-height: 1.5;">${notesHtml}</div>
      <table style="width: 100%; font-size: 12px;">
        <tbody>${keyRows}</tbody>
      </table>
    `;
  }
})();
