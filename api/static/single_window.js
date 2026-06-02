// Single-window enforcement.
// When a new tab/window opens this app, it broadcasts a "claim" message.
// Older tabs receive it and immediately redirect to /logout?reason=other_window.
// Falls back gracefully on browsers without BroadcastChannel.

(function () {
  if (typeof BroadcastChannel === "undefined") return;

  const CHANNEL = "trader_session_claim";
  const me = Math.random().toString(36).slice(2) + Date.now().toString(36);
  const bc = new BroadcastChannel(CHANNEL);

  function kickOlder() {
    // Other tabs received our claim — they evict themselves.
    if (window.__kicked) return;
    window.__kicked = true;
    try { bc.close(); } catch (_) { /* ignore */ }
    // Show a brief notice then redirect.
    if (window.toast) {
      window.toast.warn("Another window opened this app. Logging this tab out.");
    }
    setTimeout(() => {
      window.location.href = "/logout?reason=other_window";
    }, 700);
  }

  bc.onmessage = (e) => {
    const data = e.data || {};
    if (data.type === "claim" && data.id !== me) {
      kickOlder();
    }
  };

  // Announce ourselves on load + whenever the tab regains focus.
  function claim() {
    bc.postMessage({ type: "claim", id: me, ts: Date.now() });
  }
  claim();
  window.addEventListener("focus", claim);
})();
