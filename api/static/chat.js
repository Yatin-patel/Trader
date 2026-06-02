// Floating AI chat — talks to /api/chat using whatever LLM the trader is
// configured for. Conversation history is kept in localStorage so the panel
// survives page reloads.

(function () {
  const fab = document.getElementById("chat-fab");
  const panel = document.getElementById("chat-panel");
  const body = document.getElementById("chat-body");
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  const closeBtn = document.getElementById("chat-close");
  const clearBtn = document.getElementById("chat-clear");
  const providerLabel = document.getElementById("chat-provider");

  const HISTORY_KEY = "trader.chat.history.v1";
  let history = loadHistory();

  // Detect current project from the URL so chat context is project-aware.
  const projectId = (() => {
    const m = location.pathname.match(/\/projects\/([^/?#]+)/);
    return m ? decodeURIComponent(m[1]) : null;
  })();

  function loadHistory() {
    try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]"); }
    catch { return []; }
  }
  function saveHistory() {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(-40)));
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function renderMessage(role, content, cls = "") {
    const div = document.createElement("div");
    div.className = `chat-msg ${role}${cls ? " " + cls : ""}`;
    div.innerHTML = escapeHtml(content).replace(/\n/g, "<br>");
    body.appendChild(div);
    body.scrollTop = body.scrollHeight;
    return div;
  }

  function renderHistory() {
    // Reset content but keep the leading system note
    body.innerHTML = '<div class="chat-msg system">Ask anything about your trader, a recent decision, or wheel-strategy concepts.</div>';
    for (const m of history) renderMessage(m.role, m.content);
  }

  async function send(text) {
    if (!text.trim()) return;
    history.push({ role: "user", content: text });
    saveHistory();
    renderMessage("user", text);
    input.value = "";
    autosize();

    const typing = renderMessage("assistant", "thinking…", "typing");

    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text,
          history: history.slice(0, -1).slice(-20),
          project_id: projectId,
        }),
      });
      const data = await resp.json().catch(() => ({}));
      typing.remove();
      if (!resp.ok) {
        renderMessage("assistant", data.detail || "Request failed.", "error");
        return;
      }
      const reply = data.response || "(empty response)";
      history.push({ role: "assistant", content: reply });
      saveHistory();
      renderMessage("assistant", reply);
      if (data.provider) providerLabel.textContent = data.provider;
      if (window.__chat_maybe_speak) window.__chat_maybe_speak(reply);
    } catch (e) {
      typing.remove();
      renderMessage("assistant", "Network error: " + e.message, "error");
    }
  }

  // ---------- Voice (Cat 9.5) ---------------------------------------------
  const micBtn = document.getElementById("chat-mic");
  const speakBtn = document.getElementById("chat-speak");
  let speakOn = false;
  if (micBtn) {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (SR) {
      const recognition = new SR();
      recognition.continuous = false;
      recognition.interimResults = false;
      recognition.lang = "en-US";
      recognition.onresult = (e) => {
        const transcript = e.results[0][0].transcript;
        input.value = (input.value ? input.value + " " : "") + transcript;
        input.focus();
      };
      recognition.onerror = (e) => console.warn("speech error", e);
      micBtn.addEventListener("click", () => {
        try { recognition.start(); } catch (e) { /* already running */ }
      });
    } else {
      micBtn.disabled = true;
      micBtn.title = "Speech recognition not supported in this browser";
    }
  }
  if (speakBtn) {
    speakBtn.addEventListener("click", () => {
      speakOn = !speakOn;
      speakBtn.setAttribute("aria-pressed", speakOn ? "true" : "false");
      speakBtn.textContent = speakOn ? "🔇" : "🔊";
    });
  }
  window.__chat_maybe_speak = (text) => {
    if (!speakOn || !window.speechSynthesis) return;
    try {
      const u = new SpeechSynthesisUtterance(String(text || "").slice(0, 1000));
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(u);
    } catch { /* ignore */ }
  };

  // -- UI wiring --
  fab.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) input.focus();
  });
  closeBtn.addEventListener("click", () => { panel.hidden = true; });
  clearBtn.addEventListener("click", () => {
    history = [];
    saveHistory();
    renderHistory();
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    send(input.value);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send(input.value);
    }
  });
  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(120, input.scrollHeight) + "px";
  }
  input.addEventListener("input", autosize);

  renderHistory();
})();
