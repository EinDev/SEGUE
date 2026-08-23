// SEGUE — DJ view logic.
//
// No token in the URL anymore: identity comes from the Authentik
// forward-auth proxy in front of this whole app, via a trusted request
// header the api reads server-side. This page just calls the fixed
// /api/dj/me endpoint and /ws/dj websocket -- whoever is authenticated is
// whoever it is, self-registering on first visit if they're new.

(function () {
  "use strict";

  const appEl = document.getElementById("app");
  const pendingAppEl = document.getElementById("pending-app");
  const errorAppEl = document.getElementById("error-app");
  const pendingUsernameEl = document.getElementById("pending-username");
  const overlayEl = document.getElementById("conn-overlay");

  const tallyEl = document.getElementById("tally");
  const tallyLabelEl = document.getElementById("tally-label");
  const tallyNameEl = document.getElementById("tally-name");
  const liveNowEl = document.getElementById("live-now");
  const otherDjsListEl = document.getElementById("other-djs-list");

  let latestState = null;
  let deniedHard = false; // true only on 401 (no identity at all) -- not retried

  // ---- Connection resilience state ----
  let ws = null;
  let wsBackoffMs = 1000;
  const WS_BACKOFF_MAX = 15000;
  let wsReconnectTimer = null;
  let pollTimer = null;
  let lastFetchOk = true;
  let wsConnected = false;

  function wsUrl() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${window.location.host}/ws/dj`;
  }

  function setConnLost(lost) {
    document.body.classList.toggle("conn-lost", lost);
    overlayEl.classList.toggle("visible", lost);
  }

  function updateConnIndicator() {
    if (deniedHard) {
      setConnLost(false);
      return;
    }
    const down = !wsConnected && !lastFetchOk;
    setConnLost(down);
  }

  function showError() {
    deniedHard = true;
    appEl.classList.add("hidden");
    pendingAppEl.classList.add("hidden");
    errorAppEl.classList.remove("hidden");
    setConnLost(false);
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

  function showPending(username) {
    appEl.classList.add("hidden");
    errorAppEl.classList.add("hidden");
    pendingAppEl.classList.remove("hidden");
    pendingUsernameEl.textContent = username || "";
  }

  function render(state) {
    latestState = state;
    const dj = state.dj;

    if (!dj.ready) {
      showPending(dj.username);
      return;
    }

    pendingAppEl.classList.add("hidden");
    errorAppEl.classList.add("hidden");
    appEl.classList.remove("hidden");

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
    tallyNameEl.textContent = dj.username || "";

    renderLiveNow(state);
    renderOtherDjs(state);
    renderCredentials(dj.credentials);
  }

  function renderLiveNow(state) {
    const onAirUsername = state.on_air;
    if (!onAirUsername || onAirUsername === "FILLER") {
      liveNowEl.innerHTML = `Aktuell on air: <span class="filler">Filler</span>`;
      return;
    }
    // Duration is only computable for yourself (reduced state only carries
    // `since` for the viewer's own dj object, not for other usernames).
    let sinceIso = null;
    if (state.dj && state.dj.username === onAirUsername) {
      sinceIso = state.dj.since;
    }
    liveNowEl.innerHTML =
      `Aktuell on air: <span class="name">${escapeHtml(onAirUsername)}</span>` +
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
    const selfUsername = state.dj ? state.dj.username : null;
    const others = (state.djs || []).filter((d) => d.username !== selfUsername);
    otherDjsListEl.innerHTML = "";
    if (others.length === 0) {
      const li = document.createElement("li");
      li.textContent = "Keine weiteren freigeschalteten DJs.";
      otherDjsListEl.appendChild(li);
      return;
    }
    for (const d of others) {
      const li = document.createElement("li");
      const nameSpan = document.createElement("span");
      nameSpan.textContent = d.username;
      const pill = document.createElement("span");
      pill.className = "pill " + (d.connected ? "pill-connected" : "pill-disconnected");
      pill.textContent = d.connected ? "verbunden" : "nicht verbunden";
      li.appendChild(nameSpan);
      li.appendChild(pill);
      otherDjsListEl.appendChild(li);
    }
  }

  let streamKeyRevealed = false;

  function renderCredentials(creds) {
    if (!creds) return;
    setText("cred-rtmp-server", creds.rtmp_server);
    document.getElementById("cred-rtmp-server").dataset.raw = creds.rtmp_server || "";
    document.getElementById("cred-stream-key").dataset.raw = creds.stream_key || "";
    renderStreamKeyField();
    document.getElementById("cred-format-hint").textContent = creds.format_hint || "";
  }

  function renderStreamKeyField() {
    const el = document.getElementById("cred-stream-key");
    const raw = el.dataset.raw || "";
    el.textContent = streamKeyRevealed ? raw : "•".repeat(Math.max(5, raw.length || 5));
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

  document.getElementById("cred-stream-key-toggle").addEventListener("click", () => {
    streamKeyRevealed = !streamKeyRevealed;
    document.getElementById("cred-stream-key-toggle").textContent = streamKeyRevealed
      ? "Verbergen"
      : "Anzeigen";
    renderStreamKeyField();
  });

  // ---- Fetch / polling / WS plumbing ----

  async function fetchStateOnce() {
    let resp;
    try {
      resp = await fetch("/api/dj/me", { credentials: "same-origin" });
    } catch (e) {
      lastFetchOk = false;
      updateConnIndicator();
      return;
    }
    if (resp.status === 401) {
      showError();
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
    if (deniedHard) return;
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
      if (deniedHard) return;
      updateConnIndicator();
      startPolling();
      scheduleWsReconnect();
    };

    ws.onerror = () => {
      try { ws.close(); } catch (e) { /* ignore, onclose handles the rest */ }
    };
  }

  function scheduleWsReconnect() {
    if (deniedHard) return;
    if (wsReconnectTimer) return;
    wsReconnectTimer = setTimeout(() => {
      wsReconnectTimer = null;
      connectWs();
    }, wsBackoffMs);
    wsBackoffMs = Math.min(wsBackoffMs * 2, WS_BACKOFF_MAX);
  }

  // ---- Boot ----

  fetchStateOnce().then(() => {
    if (!deniedHard) {
      startPolling(); // active until WS confirms it's up
      connectWs();
    }
  });

  setInterval(tickSince, 1000);
})();
