// Project sub-nav badge poller.
// Updates market-badge, runner-badge, and last-update by polling /snapshot.
// Runs on every project-scoped page (dashboard, performance, analysis, …).

(function () {
  const subnav = document.querySelector(".project-subnav");
  if (!subnav) return;
  const projectId = subnav.dataset.projectId;
  if (!projectId) return;
  const enc = encodeURIComponent(projectId);

  const marketBadge = document.getElementById("market-badge");
  const runnerBadge = document.getElementById("runner-badge");
  const lastUpdate = document.getElementById("last-update");

  async function tick() {
    try {
      const r = await fetch(`/api/projects/${enc}/snapshot`, { cache: "no-store" });
      if (!r.ok) throw new Error(r.statusText || ("HTTP " + r.status));
      const snap = await r.json();

      if (marketBadge) {
        const isOpen = snap.clock && snap.clock.is_open;
        marketBadge.textContent = isOpen ? "MARKET OPEN" : "MARKET CLOSED";
        marketBadge.className = "badge " + (isOpen ? "ok" : "warn");
      }
      if (runnerBadge) {
        const active = snap.project && snap.project.is_active;
        runnerBadge.textContent = active ? "runner: ACTIVE" : "runner: INACTIVE";
        runnerBadge.className = "badge " + (active ? "ok" : "danger");
      }
      if (lastUpdate) {
        lastUpdate.textContent = "updated " + new Date().toLocaleTimeString();
      }
    } catch (e) {
      if (lastUpdate) lastUpdate.textContent = "update failed";
    }
  }

  tick();
  setInterval(tick, 5000);
})();
