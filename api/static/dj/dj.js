// SEGUE — DJ view logic.
// Token comes from the URL path (/dj/{token}), never hardcoded.

(function () {
  "use strict";

  const token = decodeURIComponent(
    window.location.pathname.split("/").filter(Boolean).pop() || ""
  );

  const appEl = document.getElementById("app");
  const errorAppEl = document.getElementById("error-app");
  const overlayEl = document.getElementById("conn-overlay");

  const tallyEl = document.getElementById("tally");
  const tallyLabelEl = document.getElementById("tally-label");
  const tallyNameEl = document.getElementById("tally-name");
  const liveNowEl = document.getElementById("live-now");
  const otherDjsListEl = document.getElementById("other-djs-list");

  let latestState = null;
  let sinceTickHandle = null;
  let invalidToken = false;

  // ---- Connection resilience state ----
  let ws = null;
  let wsBackoffMs = 1000;
  const WS_BACKOFF_MAX = 15000;
  let wsReconnectTimer = null;
  let pollTimer = null;
  let lastFetchOk = true; // becomes false when a poll/fetch errors out
  let wsConnected = false;

  function apiStateUrl() {
    return `/api/dj/${encodeURIComponent(token)}/state`;
  }

  function wsUrl() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${window.location.host}/ws/dj/${encodeURIComponent(token)}`;
  }

  function setConnLost(lost) {
    document.body.classList.toggle("conn-lost", lost);
    overlayEl.classList.toggle("visible", lost);
  }

  function updateConnIndicator() {
    // "Down" means: WS is not currently connected AND the fallback poll
    // itself is failing (or hasn't succeeded).
    const down = !wsConnected && !lastFetchOk;
    setConnLost(down);
  }

  function showInvalidToken() {
    invalidToken = true;
    appEl.classList.add("hidden");
    errorAppEl.classList.remove("hidden");
    setConnLost(false); // don't show "connection lost" over an intentional error state
    stopPolling();
    if (ws) {
      try { ws.close(); } catch (e) { /* ignore */ }
      ws = null;
    }
    if (wsReconnectTimer) {
      clearTimeout(wsReconnectTimer);
      wsReconnectTimer = null;
    }
  }

  function render(state) {
    latestState = state;
    appEl.classList.remove("hidden");
    errorAppEl.classList.add("hidden");

    const dj = state.dj;

    // Tally
    tallyEl.classList.remove("state-disconnected", "state-connected", "state-onair");
    if (dj.connected === false) {
      tallyEl.classList.add("state-disconnected");
      tallyLabelEl.textContent = "NICHT VERBUNDEN";
    } else if (dj.on_air === true) {
      tallyEl.classList.add("state-onair");
      tallyLabelEl.textContent = "ON AIR";
    } else {
      tallyEl.classList.add("state-connected");
      tallyLabelEl.textContent = "VERBUNDEN — NICHT ON AIR";
    }
    tallyNameEl.textContent = dj.name || "";

    renderLiveNow(state);
    renderOtherDjs(state);
    renderCredentials(dj.credentials);
  }

  function renderLiveNow(state) {
    const onAirId = state.on_air;
    if (!onAirId || onAirId === "FILLER") {
      liveNowEl.innerHTML = `Aktuell on air: <span class="filler">Filler</span>`;
      return;
    }
    const djInfo = (state.djs || []).find((d) => d.id === onAirId);
    const name = djInfo ? djInfo.name : onAirId;
    // Duration: only computable if it's this DJ (we have "since"), otherwise
    // fall back to no duration since reduced state only carries `since` for self.
    let sinceIso = null;
    if (state.dj && state.dj.id === onAirId) {
      sinceIso = state.dj.since;
    }
    liveNowEl.innerHTML =
      `Aktuell on air: <span class="name">${escapeHtml(name)}</span>` +
      (sinceIso ? ` <span id="live-since-suffix"></span>` : "");
    liveNowEl.dataset.sinceIso = sinceIso || "";
  }

  function tickSince() {
    if (!latestState) return;
    const iso = liveNowEl.dataset.sinceIso;
    const suffix = document.getElementById("live-since-suffix");
    if (!iso || !suffix) return;
    const since = new Date(iso).getTime();
    if (Number.isNaN(since)) return;
    const deltaSec = Math.max(0, Math.floor((Date.now() - since) / 1000));
    const m = Math.floor(deltaSec / 60);
    const s = deltaSec % 60;
    suffix.textContent = `— seit ${m}m ${s}s`;
  }

  function renderOtherDjs(state) {
    const selfId = state.dj ? state.dj.id : null;
    const others = (state.djs || []).filter((d) => d.id !== selfId);
    otherDjsListEl.innerHTML = "";
    if (others.length === 0) {
      const li = document.createElement("li");
      li.textContent = "Keine weiteren DJs konfiguriert.";
      otherDjsListEl.appendChild(li);
      return;
    }
    for (const d of others) {
      const li = document.createElement("li");
      const nameSpan = document.createElement("span");
      nameSpan.textContent = d.name;
      const pill = document.createElement("span");
      pill.className = "pill " + (d.connected ? "pill-connected" : "pill-disconnected");
      pill.textContent = d.connected ? "verbunden" : "nicht verbunden";
      li.appendChild(nameSpan);
      li.appendChild(pill);
      otherDjsListEl.appendChild(li);
    }
  }

  let passwordRevealed = false;

  function renderCredentials(creds) {
    if (!creds) return;
    setText("cred-host", creds.host);
    setText("cred-port", creds.port);
    setText("cred-mount", creds.mount);
    setText("cred-user", creds.user);
    document.getElementById("cred-password").dataset.raw = creds.password || "";
    document.getElementById("cred-host").dataset.raw = creds.host || "";
    document.getElementById("cred-port").dataset.raw = String(creds.port != null ? creds.port : "");
    document.getElementById("cred-mount").dataset.raw = creds.mount || "";
    document.getElementById("cred-user").dataset.raw = creds.user || "";
    renderPasswordField();
    document.getElementById("cred-format-hint").textContent = creds.format_hint || "";
  }

  function renderPasswordField() {
    const el = document.getElementById("cred-password");
    const raw = el.dataset.raw || "";
    el.textContent = passwordRevealed ? raw : "•".repeat(Math.max(5, raw.length || 5));
  }

  function setText(id, value) {
    document.getElementById(id).textContent = value != null ? String(value) : "";
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  // ---- Copy buttons ----
  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".cred-copy-btn");
    if (!btn) return;
    const targetId = btn.dataset.copyTarget;
    const el = document.getElementById(targetId);
    if (!el) return;
    const text = el.dataset.raw != null ? el.dataset.raw : el.textContent;
    navigator.clipboard.writeText(text).then(
      () => flashCopied(btn),
      () => flashCopied(btn, true)
    );
  });

  function flashCopied(btn, failed) {
    const original = btn.textContent;
    btn.textContent = failed ? "Fehler" : "Kopiert!";
    setTimeout(() => {
      btn.textContent = original;
    }, 1000);
  }

  document.getElementById("cred-password-toggle").addEventListener("click", () => {
    passwordRevealed = !passwordRevealed;
    document.getElementById("cred-password-toggle").textContent = passwordRevealed
      ? "Verbergen"
      : "Anzeigen";
    renderPasswordField();
  });

  // ---- Fetch / polling / WS plumbing ----

  async function fetchStateOnce() {
    let resp;
    try {
      resp = await fetch(apiStateUrl(), { credentials: "same-origin" });
    } catch (e) {
      lastFetchOk = false;
      updateConnIndicator();
      return;
    }
    if (resp.status === 404) {
      showInvalidToken();
      return;
    }
    if (!resp.ok) {
      lastFetchOk = false;
      updateConnIndicator();
      return;
    }
    lastFetchOk = true;
    updateConnIndicator();
    try {
      const data = await resp.json();
      render(data);
    } catch (e) {
      lastFetchOk = false;
      updateConnIndicator();
    }
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(fetchStateOnce, 3000);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function connectWs() {
    if (invalidToken) return;
    try {
      ws = new WebSocket(wsUrl());
    } catch (e) {
      scheduleWsReconnect();
      return;
    }

    ws.onopen = () => {
      wsConnected = true;
      wsBackoffMs = 1000;
      updateConnIndicator();
      // WS is live: drop back to WS-only push.
      stopPolling();
    };

    ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        render(data);
        lastFetchOk = true;
        updateConnIndicator();
      } catch (e) {
        // ignore malformed message
      }
    };

    ws.onclose = () => {
      wsConnected = false;
      if (invalidToken) return; // don't resurrect polling under the error page
      updateConnIndicator();
      startPolling(); // fallback while WS is down
      scheduleWsReconnect();
    };

    ws.onerror = () => {
      try { ws.close(); } catch (e) { /* ignore, onclose handles the rest */ }
    };
  }

  function scheduleWsReconnect() {
    if (invalidToken) return;
    if (wsReconnectTimer) return;
    wsReconnectTimer = setTimeout(() => {
      wsReconnectTimer = null;
      connectWs();
    }, wsBackoffMs);
    wsBackoffMs = Math.min(wsBackoffMs * 2, WS_BACKOFF_MAX);
  }

  // ---- Boot ----

  if (!token) {
    showInvalidToken();
  } else {
    fetchStateOnce().then(() => {
      if (!invalidToken) {
        startPolling(); // active until WS confirms it's up
        connectWs();
      }
    });
  }

  sinceTickHandle = setInterval(tickSince, 1000);
})();
