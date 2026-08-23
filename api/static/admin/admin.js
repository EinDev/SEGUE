// SEGUE — Admin view logic.

(function () {
  "use strict";

  const loginAppEl = document.getElementById("login-app");
  const loginFormEl = document.getElementById("login-box");
  const loginTokenEl = document.getElementById("login-token");
  const loginErrorEl = document.getElementById("login-error");

  const appEl = document.getElementById("app");
  const overlayEl = document.getElementById("conn-overlay");

  const modeAutoBtn = document.getElementById("mode-auto-btn");
  const modeManualBtn = document.getElementById("mode-manual-btn");
  const reasonTextEl = document.getElementById("reason-text");
  const clockEl = document.getElementById("clock");
  const warningBannerEl = document.getElementById("warning-banner");
  const warningTextEl = document.getElementById("warning-text");
  const fillerBtn = document.getElementById("filler-btn");
  const djRowsEl = document.getElementById("dj-rows");
  const eventlogEl = document.getElementById("eventlog");

  let authenticated = false;
  let latestState = null;

  // ---- Clock: seeded from server_time, ticks locally ----
  let clockOffsetMs = 0; // server_time - local Date.now() at time of last state push
  let clockTimer = null;

  function seedClock(serverTimeIso) {
    const serverMs = new Date(serverTimeIso).getTime();
    if (!Number.isNaN(serverMs)) {
      clockOffsetMs = serverMs - Date.now();
    }
  }

  function tickClock() {
    const now = new Date(Date.now() + clockOffsetMs);
    const hh = String(now.getHours()).padStart(2, "0");
    const mm = String(now.getMinutes()).padStart(2, "0");
    const ss = String(now.getSeconds()).padStart(2, "0");
    clockEl.textContent = `${hh}:${mm}:${ss}`;
  }

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
    return `${proto}//${window.location.host}/ws`;
  }

  function setConnLost(lost) {
    document.body.classList.toggle("conn-lost", lost);
    overlayEl.classList.toggle("visible", lost);
  }

  function updateConnIndicator() {
    if (!authenticated) {
      setConnLost(false);
      return;
    }
    const down = !wsConnected && !lastFetchOk;
    setConnLost(down);
  }

  // ---- Login ----

  loginFormEl.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    loginErrorEl.textContent = "";
    const token = loginTokenEl.value;
    if (!token) return;
    let resp;
    try {
      resp = await fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ token }),
      });
    } catch (e) {
      loginErrorEl.textContent = "Server nicht erreichbar.";
      return;
    }
    if (resp.status === 401) {
      loginErrorEl.textContent = "Falsches Token.";
      return;
    }
    if (!resp.ok) {
      loginErrorEl.textContent = "Unerwarteter Fehler.";
      return;
    }
    onAuthenticated();
  });

  function showLogin() {
    authenticated = false;
    stopPolling();
    if (ws) {
      try { ws.close(); } catch (e) { /* ignore */ }
      ws = null;
    }
    if (wsReconnectTimer) {
      clearTimeout(wsReconnectTimer);
      wsReconnectTimer = null;
    }
    if (clockTimer) {
      clearInterval(clockTimer);
      clockTimer = null;
    }
    setConnLost(false);
    appEl.classList.add("hidden");
    loginAppEl.classList.remove("hidden");
    loginTokenEl.value = "";
    loginTokenEl.focus();
  }

  function onAuthenticated() {
    authenticated = true;
    loginAppEl.classList.add("hidden");
    appEl.classList.remove("hidden");
    wsBackoffMs = 1000;
    fetchStateOnce().then(() => {
      startPolling();
      connectWs();
    });
    fetchLog();
    if (!clockTimer) {
      clockTimer = setInterval(tickClock, 1000);
    }
  }

  // ---- Rendering ----

  function render(state) {
    latestState = state;
    seedClock(state.server_time);
    tickClock();

    modeAutoBtn.classList.toggle("active", state.mode === "AUTO");
    modeManualBtn.classList.toggle("active", state.mode === "MANUAL");
    reasonTextEl.textContent = state.reason || "";

    if (state.warning) {
      warningBannerEl.classList.remove("hidden");
      warningTextEl.textContent = state.warning;
    } else {
      warningBannerEl.classList.add("hidden");
      warningTextEl.textContent = "";
    }

    renderDjRows(state);
  }

  function renderDjRows(state) {
    djRowsEl.innerHTML = "";
    for (const dj of state.djs || []) {
      const row = document.createElement("div");
      row.className = "dj-row" + (state.on_air === dj.id ? " on-air" : "");

      const name = document.createElement("div");
      name.className = "dj-name";
      name.textContent = dj.name;

      const pill = document.createElement("span");
      pill.className = "pill " + (dj.connected ? "pill-connected" : "pill-disconnected");
      pill.textContent = dj.connected ? "verbunden" : "nicht verbunden";

      const since = document.createElement("div");
      since.className = "dj-since";
      since.textContent = dj.connected ? formatSince(dj.since) : "";

      const spacer = document.createElement("div");
      spacer.className = "spacer";

      const btn = document.createElement("button");
      btn.className = "onair-btn";
      btn.textContent = "On Air schalten";
      btn.disabled = !dj.connected;
      btn.addEventListener("click", () => pinDj(dj.id));

      row.appendChild(name);
      row.appendChild(pill);
      row.appendChild(since);
      row.appendChild(spacer);
      row.appendChild(btn);
      djRowsEl.appendChild(row);
    }
  }

  function formatSince(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `seit ${hh}:${mm}`;
  }

  function renderLog(entries) {
    eventlogEl.innerHTML = "";
    if (!entries || entries.length === 0) {
      const row = document.createElement("div");
      row.className = "text-faint";
      row.textContent = "Keine Einträge.";
      eventlogEl.appendChild(row);
      return;
    }
    // Most-recent-first at the top.
    for (const entry of entries) {
      const row = document.createElement("div");
      row.className = "log-row";
      const ts = document.createElement("span");
      ts.className = "log-ts";
      ts.textContent = formatLogTs(entry.ts);
      const msg = document.createElement("span");
      msg.textContent = entry.message;
      row.appendChild(ts);
      row.appendChild(msg);
      eventlogEl.appendChild(row);
    }
  }

  function formatLogTs(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ss = String(d.getSeconds()).padStart(2, "0");
    return `${hh}:${mm}:${ss}`;
  }

  // ---- Actions ----

  async function authedFetch(url, opts) {
    let resp;
    try {
      resp = await fetch(url, Object.assign({ credentials: "same-origin" }, opts));
    } catch (e) {
      lastFetchOk = false;
      updateConnIndicator();
      throw e;
    }
    if (resp.status === 401) {
      showLogin();
      throw new Error("unauthorized");
    }
    lastFetchOk = resp.ok;
    updateConnIndicator();
    return resp;
  }

  modeAutoBtn.addEventListener("click", () => setMode("AUTO"));
  modeManualBtn.addEventListener("click", () => setMode("MANUAL"));

  async function setMode(mode) {
    try {
      await authedFetch("/api/mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
    } catch (e) { /* handled via authedFetch */ }
  }

  async function pinDj(djId) {
    try {
      await authedFetch("/api/pin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dj_id: djId }),
      });
    } catch (e) { /* handled via authedFetch */ }
  }

  fillerBtn.addEventListener("click", async () => {
    try {
      await authedFetch("/api/filler", { method: "POST" });
    } catch (e) { /* handled via authedFetch */ }
  });

  // ---- Fetch / polling / WS plumbing ----

  async function fetchStateOnce() {
    let resp;
    try {
      resp = await authedFetch("/api/state");
    } catch (e) {
      return;
    }
    try {
      const data = await resp.json();
      render(data);
    } catch (e) {
      lastFetchOk = false;
      updateConnIndicator();
    }
  }

  let logDebounceTimer = null;
  function fetchLogDebounced() {
    if (logDebounceTimer) return;
    logDebounceTimer = setTimeout(() => {
      logDebounceTimer = null;
      fetchLog();
    }, 400);
  }

  async function fetchLog() {
    let resp;
    try {
      resp = await authedFetch("/api/log?limit=50");
    } catch (e) {
      return;
    }
    try {
      const entries = await resp.json();
      renderLog(entries);
    } catch (e) {
      // non-fatal for the connection indicator; log is secondary
    }
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(() => {
      fetchStateOnce();
      fetchLog();
    }, 3000);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function connectWs() {
    if (!authenticated) return;
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
        // The eventlog isn't part of the pushed state; refresh it opportunistically
        // (debounced so a burst of pushes doesn't hammer the endpoint).
        fetchLogDebounced();
      } catch (e) {
        // ignore malformed message
      }
    };

    ws.onclose = () => {
      wsConnected = false;
      updateConnIndicator();
      if (authenticated) {
        startPolling();
        scheduleWsReconnect();
      }
    };

    ws.onerror = () => {
      try { ws.close(); } catch (e) { /* ignore, onclose handles the rest */ }
    };
  }

  function scheduleWsReconnect() {
    if (!authenticated) return;
    if (wsReconnectTimer) return;
    wsReconnectTimer = setTimeout(() => {
      wsReconnectTimer = null;
      connectWs();
    }, wsBackoffMs);
    wsBackoffMs = Math.min(wsBackoffMs * 2, WS_BACKOFF_MAX);
  }

  // ---- Boot: try a state fetch; 401 means "show login", success means
  // a session cookie already existed (e.g. page refresh). ----

  (async function boot() {
    let resp;
    try {
      resp = await fetch("/api/state", { credentials: "same-origin" });
    } catch (e) {
      // Can't reach the server at all; show login, resilience layer will
      // report connection loss once authenticated.
      showLogin();
      return;
    }
    if (resp.ok) {
      onAuthenticated();
    } else {
      showLogin();
    }
  })();
})();
