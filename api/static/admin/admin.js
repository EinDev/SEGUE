// SEGUE — Admin view logic.
//
// No login form: this page sits behind Authentik forward-auth (configured
// at the Coolify/Traefik level), so by the time a request reaches this app
// it's already an authenticated human. The only question left is whether
// that human is *the* admin (ONAIR_ADMIN_USERNAME) -- if not, the api
// answers every /api/* call with 403 and this page just shows a denied
// screen instead of the dashboard. There is nothing to log in *to* here.

(function () {
  "use strict";

  const deniedEl = document.getElementById("denied-app");
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
  const rosterRowsEl = document.getElementById("roster-rows");
  const eventlogEl = document.getElementById("eventlog");

  let authorized = false;
  let rosterError = null; // { username, message } shown inline on that row

  // ---- Clock: seeded from server_time, ticks locally ----
  let clockOffsetMs = 0;
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
    if (!authorized) {
      setConnLost(false);
      return;
    }
    const down = !wsConnected && !lastFetchOk;
    setConnLost(down);
  }

  function showDenied() {
    authorized = false;
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
    deniedEl.classList.remove("hidden");
  }

  function onAuthorized() {
    authorized = true;
    deniedEl.classList.add("hidden");
    appEl.classList.remove("hidden");
    wsBackoffMs = 1000;
    fetchStateOnce().then(() => {
      startPolling();
      connectWs();
    });
    fetchLog();
    fetchRoster();
    if (!clockTimer) {
      clockTimer = setInterval(tickClock, 1000);
    }
  }

  // ---- Rendering: on-air state ----

  function render(state) {
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
    const djs = state.djs || [];
    if (djs.length === 0) {
      const empty = document.createElement("div");
      empty.className = "text-faint";
      empty.textContent = "Noch keine freigeschalteten DJs.";
      djRowsEl.appendChild(empty);
      return;
    }
    for (const dj of djs) {
      const row = document.createElement("div");
      row.className = "dj-row" + (state.on_air === dj.username ? " on-air" : "");

      const name = document.createElement("div");
      name.className = "dj-name";
      name.textContent = dj.username;

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
      btn.addEventListener("click", () => pinDj(dj.username));

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

  // ---- Rendering: DJ roster (registration + ready approval) ----

  function renderRoster(djs) {
    rosterRowsEl.innerHTML = "";
    if (!djs || djs.length === 0) {
      const empty = document.createElement("div");
      empty.className = "text-faint";
      empty.textContent = "Noch niemand hat sich über den DJ-Link angemeldet.";
      rosterRowsEl.appendChild(empty);
      return;
    }
    for (const dj of djs) {
      const row = document.createElement("div");
      row.className = "roster-row";

      const name = document.createElement("div");
      name.className = "username";
      name.textContent = dj.username;

      const pill = document.createElement("span");
      pill.className = "pill " + (dj.connected ? "pill-connected" : "pill-disconnected");
      pill.textContent = dj.connected ? "verbunden" : "nicht verbunden";

      const slot = document.createElement("div");
      slot.className = "slot";
      slot.textContent = dj.slot || "";

      const spacer = document.createElement("div");
      spacer.className = "spacer";

      const toggle = document.createElement("button");
      toggle.className = "ready-toggle" + (dj.ready ? " on" : "");
      toggle.textContent = dj.ready ? "Bereit" : "Nicht bereit";
      toggle.addEventListener("click", () => setReady(dj.username, !dj.ready));

      row.appendChild(name);
      row.appendChild(pill);
      row.appendChild(slot);
      row.appendChild(spacer);
      row.appendChild(toggle);

      if (rosterError && rosterError.username === dj.username) {
        const err = document.createElement("div");
        err.className = "roster-error";
        err.textContent = rosterError.message;
        row.appendChild(err);
      }

      rosterRowsEl.appendChild(row);
    }
  }

  async function setReady(username, ready) {
    rosterError = null;
    try {
      const resp = await authedFetch(`/api/djs/${encodeURIComponent(username)}/ready`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ready }),
      });
      if (resp.status === 409) {
        const body = await resp.json().catch(() => ({}));
        rosterError = { username, message: body.detail || "Kein freier Slot verfügbar." };
      }
    } catch (e) {
      // handled via authedFetch (401) or network error; nothing else to do
    }
    fetchRoster();
  }

  // ---- Eventlog ----

  function renderLog(entries) {
    eventlogEl.innerHTML = "";
    if (!entries || entries.length === 0) {
      const row = document.createElement("div");
      row.className = "text-faint";
      row.textContent = "Keine Einträge.";
      eventlogEl.appendChild(row);
      return;
    }
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
    if (resp.status === 401 || resp.status === 403) {
      showDenied();
      throw new Error("not authorized");
    }
    lastFetchOk = resp.ok || resp.status === 409; // a 409 is a valid, connected response
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

  async function pinDj(username) {
    try {
      await authedFetch("/api/pin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username }),
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

  async function fetchRoster() {
    let resp;
    try {
      resp = await authedFetch("/api/djs");
    } catch (e) {
      return;
    }
    try {
      const djs = await resp.json();
      renderRoster(djs);
    } catch (e) {
      // non-fatal for the connection indicator
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
      fetchRoster();
    }, 3000);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function connectWs() {
    if (!authorized) return;
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
        // Eventlog and the DJ roster aren't part of the pushed state; refresh
        // them opportunistically (debounced so a burst of pushes doesn't
        // hammer the endpoints).
        fetchLogDebounced();
      } catch (e) {
        // ignore malformed message
      }
    };

    ws.onclose = () => {
      wsConnected = false;
      updateConnIndicator();
      if (authorized) {
        startPolling();
        scheduleWsReconnect();
      }
    };

    ws.onerror = () => {
      try { ws.close(); } catch (e) { /* ignore, onclose handles the rest */ }
    };
  }

  function scheduleWsReconnect() {
    if (!authorized) return;
    if (wsReconnectTimer) return;
    wsReconnectTimer = setTimeout(() => {
      wsReconnectTimer = null;
      connectWs();
    }, wsBackoffMs);
    wsBackoffMs = Math.min(wsBackoffMs * 2, WS_BACKOFF_MAX);
  }

  // ---- Boot: a 200 on /api/state means the Authentik-authenticated user
  // is the admin; 401/403 means either "not behind the proxy" or "not the
  // admin" -- either way, show the denied screen instead of a login form,
  // since there is nothing this page can do to log the user in itself. ----

  (async function boot() {
    let resp;
    try {
      resp = await fetch("/api/state", { credentials: "same-origin" });
    } catch (e) {
      showDenied();
      return;
    }
    if (resp.ok) {
      onAuthorized();
    } else {
      showDenied();
    }
  })();
})();
