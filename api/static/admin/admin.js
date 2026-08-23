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
  let rosterByUsername = {}; // username -> {username, ready, slot, connected, created_at}
  let latestAdminState = null;
  const expandedDetails = new Set(); // usernames with the details panel open
  const activePreviews = new Map(); // username -> Hls instance, or `true` for native HLS playback

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
    teardownAllPreviews();
    for (const username of Array.from(detailStatsTimers.keys())) {
      stopDetailStatsPolling(username);
    }
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
    fetchAdminInfo();
    if (!clockTimer) {
      clockTimer = setInterval(tickClock, 1000);
    }
  }

  // ---- Static admin-only info (RTMP server address) - fetched once, not
  // part of the polled/pushed state since it never changes at runtime ----

  async function fetchAdminInfo() {
    let resp;
    try {
      resp = await authedFetch("/api/admin/info");
    } catch (e) {
      return;
    }
    try {
      const data = await resp.json();
      const el = document.getElementById("rtmp-server-value");
      if (el) el.textContent = data.rtmp_server || "—";
    } catch (e) {
      // non-fatal, informational only
    }
  }

  // ---- Rendering: on-air state ----

  function render(state) {
    latestAdminState = state;
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

  // renderDjRows rebuilds row *content* every poll/push, but reuses
  // existing row/details DOM nodes (keyed by data-username) instead of
  // tearing the whole list down - a from-scratch rebuild every ~3s would
  // kill and re-attach any open live-preview <video>/Hls instance
  // constantly, which is both wasteful and visibly glitchy.
  function renderDjRows(state) {
    const djs = state.djs || [];
    if (djs.length === 0) {
      teardownAllPreviews();
      djRowsEl.innerHTML = "";
      const empty = document.createElement("div");
      empty.className = "text-faint";
      empty.textContent = "Noch keine freigeschalteten DJs.";
      djRowsEl.appendChild(empty);
      return;
    }
    const placeholder = djRowsEl.querySelector(".text-faint");
    if (placeholder) placeholder.remove();

    const seen = new Set();
    for (const dj of djs) {
      seen.add(dj.username);
      let row = findDjRow(dj.username);
      if (!row) {
        row = buildDjRow(dj.username);
        djRowsEl.appendChild(row);
      }
      updateDjRow(row, dj, state);
    }
    for (const row of Array.from(djRowsEl.querySelectorAll(".dj-row"))) {
      if (!seen.has(row.dataset.username)) {
        teardownPreview(row.dataset.username);
        stopDetailStatsPolling(row.dataset.username);
        expandedDetails.delete(row.dataset.username);
        row.remove();
      }
    }
  }

  function findDjRow(username) {
    for (const row of djRowsEl.querySelectorAll(".dj-row")) {
      if (row.dataset.username === username) return row;
    }
    return null;
  }

  function buildDjRow(username) {
    const row = document.createElement("div");
    row.className = "dj-row";
    row.dataset.username = username;

    const top = document.createElement("div");
    top.className = "dj-row-top";

    const name = document.createElement("div");
    name.className = "dj-name";
    name.textContent = username;

    const pill = document.createElement("span");
    pill.className = "pill";

    const since = document.createElement("div");
    since.className = "dj-since";

    const spacer = document.createElement("div");
    spacer.className = "spacer";

    const detailsBtn = document.createElement("button");
    detailsBtn.className = "details-btn";
    detailsBtn.textContent = "Details";
    detailsBtn.addEventListener("click", () => toggleDetails(username));

    const onairBtn = document.createElement("button");
    onairBtn.className = "onair-btn";
    onairBtn.textContent = "On Air schalten";
    onairBtn.addEventListener("click", () => pinDj(username));

    top.appendChild(name);
    top.appendChild(pill);
    top.appendChild(since);
    top.appendChild(spacer);
    top.appendChild(detailsBtn);
    top.appendChild(onairBtn);

    const details = document.createElement("div");
    details.className = "dj-details hidden";
    details.innerHTML =
      '<div class="dj-details-grid">' +
      djDetailField("resolution", "Auflösung") +
      djDetailField("codec", "Codec") +
      djDetailField("bitrate", "Bitrate") +
      djDetailField("delay", "Verzögerung DJ→Server") +
      djDetailField("since", "Verbunden") +
      djDetailField("remote", "Adresse") +
      djDetailField("agent", "Encoder") +
      "</div>" +
      '<div class="chart-block">' +
      '<div class="chart-block-label">Bitrate (5 Min.)</div>' +
      '<div class="chart-svg-wrap" data-chart="bitrate"></div>' +
      '<div class="chart-caption" data-chart-caption="bitrate"></div>' +
      "</div>" +
      '<div class="chart-block">' +
      '<div class="chart-block-label">Verzögerung DJ→Server (5 Min.)</div>' +
      '<div class="chart-svg-wrap" data-chart="delay"></div>' +
      '<div class="chart-caption" data-chart-caption="delay"></div>' +
      "</div>" +
      '<button class="preview-btn">Vorschau anzeigen</button>' +
      '<div class="preview-wrap hidden"><video class="preview-video" muted playsinline autoplay></video></div>';
    details.querySelector(".preview-btn").addEventListener("click", () => togglePreview(username, row));

    row.appendChild(top);
    row.appendChild(details);
    return row;
  }

  function djDetailField(field, label) {
    return (
      '<div class="cred-row"><div class="cred-label">' +
      escapeHtml(label) +
      '</div><div class="cred-value" data-field="' +
      field +
      '">—</div></div>'
    );
  }

  function updateDjRow(row, dj, state) {
    row.classList.toggle("on-air", state.on_air === dj.username);
    row.querySelector(".dj-name").textContent = dj.username;

    const pill = row.querySelector(".pill");
    pill.className = "pill " + (dj.connected ? "pill-connected" : "pill-disconnected");
    pill.textContent = dj.connected ? "verbunden" : "nicht verbunden";

    row.querySelector(".dj-since").textContent = dj.connected ? formatSince(dj.since) : "";
    row.querySelector(".onair-btn").disabled = !dj.connected;

    const isExpanded = expandedDetails.has(dj.username);
    const detailsEl = row.querySelector(".dj-details");
    detailsEl.classList.toggle("hidden", !isExpanded);
    row.querySelector(".details-btn").textContent = isExpanded ? "Details ausblenden" : "Details";

    if (isExpanded) {
      startDetailStatsPolling(dj.username, detailsEl); // idempotent if already running
    } else {
      stopDetailStatsPolling(dj.username);
    }
    if (!dj.connected) {
      // A disconnected DJ's preview would just spin forever - drop it
      // rather than leave a dead player attached.
      teardownPreview(dj.username, detailsEl);
    }
  }

  // ---- Per-DJ details polling (instantaneous stats + 5-min history) ----
  //
  // Dedicated interval per expanded DJ, independent of the ~3s admin
  // state poll/push cadence (which is irregular - WS pushes fire on any
  // state change, not on a steady clock). A chart needs evenly-spaced
  // samples; this mirrors dj.js's own STREAM_STATS_INTERVAL_MS pattern
  // for its "Verbindungsqualität" card against the same endpoint shape.
  // Only runs while a DJ's details panel is actually open - stopped on
  // collapse, on disconnect, and when the row itself is torn down.

  const DETAIL_STATS_INTERVAL_MS = 8000;
  const detailStatsTimers = new Map(); // username -> intervalId

  function startDetailStatsPolling(username, detailsEl) {
    if (detailStatsTimers.has(username)) return;
    const tick = () => fetchAndRenderDetailStats(username, detailsEl);
    tick();
    detailStatsTimers.set(username, setInterval(tick, DETAIL_STATS_INTERVAL_MS));
  }

  function stopDetailStatsPolling(username) {
    const timer = detailStatsTimers.get(username);
    if (timer) clearInterval(timer);
    detailStatsTimers.delete(username);
  }

  async function fetchAndRenderDetailStats(username, detailsEl) {
    let resp;
    try {
      resp = await authedFetch(`/api/admin/stream/${encodeURIComponent(username)}`);
    } catch (e) {
      return;
    }
    let data = null;
    try {
      data = await resp.json();
    } catch (e) {
      return;
    }
    renderDjDetails(detailsEl, data);
  }

  function renderDjDetails(detailsEl, data) {
    const set = (field, value) => {
      const el = detailsEl.querySelector(`[data-field="${field}"]`);
      if (el) el.textContent = value;
    };
    if (!data || !data.connected) {
      set("resolution", "—");
      set("codec", "—");
      set("bitrate", "—");
      set("delay", "—");
      set("since", "—");
      set("remote", "—");
      set("agent", "—");
    } else {
      set("resolution", data.resolution || "unbekannt");
      set("codec", [data.video_codec, data.audio_codec].filter(Boolean).join(" / ") || "unbekannt");
      set("bitrate", data.bitrate_kbps != null ? `${data.bitrate_kbps} kbit/s` : "wird berechnet…");
      // Same figure as the DJ's own "Verbindungsqualität" card - this is
      // DJ-encoder-to-relay delay only, not end-to-end to VRCDN (see
      // mediamtx_stats.py's module docstring for why that isn't
      // measurable from here).
      set("delay", data.delay_seconds != null ? `${data.delay_seconds.toFixed(1)} s` : "unbekannt");
      set("since", data.connected_since ? formatSince(data.connected_since) : "unbekannt");
      set("remote", data.remote_addr || "unbekannt");
      set("agent", data.user_agent || "unbekannt");
    }

    // 5-min trend charts - "history" is server-side sampled/stored (see
    // api/app/main.py's _history_collector_loop), shared across every
    // viewer rather than accumulated in this tab.
    const history = data && data.history;
    window.SegueChart.renderSparkline(
      detailsEl.querySelector('[data-chart="bitrate"]'),
      detailsEl.querySelector('[data-chart-caption="bitrate"]'),
      window.SegueChart.toSeries(history, "bitrate_kbps"),
      { unit: " kbit/s", decimals: 0, colorClass: "chart-line--bitrate" }
    );
    window.SegueChart.renderSparkline(
      detailsEl.querySelector('[data-chart="delay"]'),
      detailsEl.querySelector('[data-chart-caption="delay"]'),
      window.SegueChart.toSeries(history, "delay_seconds"),
      { unit: " s", decimals: 2, colorClass: "chart-line--delay" }
    );
  }

  function toggleDetails(username) {
    if (expandedDetails.has(username)) {
      expandedDetails.delete(username);
      teardownPreview(username);
      stopDetailStatsPolling(username);
    } else {
      expandedDetails.add(username);
    }
    if (latestAdminState) renderDjRows(latestAdminState);
  }

  // ---- Live preview (admin-only HLS proxy, see app.main's
  // /api/admin/preview/{slot}/... - lazy-attached per DJ on click, never
  // auto-played for every connected DJ at once) ----

  function togglePreview(username, row) {
    const roster = rosterByUsername[username];
    if (!roster || !roster.slot) return;
    const wrap = row.querySelector(".preview-wrap");
    const btn = row.querySelector(".preview-btn");

    if (activePreviews.has(username)) {
      teardownPreview(username, row.querySelector(".dj-details"));
      return;
    }

    wrap.classList.remove("hidden");
    btn.textContent = "Vorschau ausblenden";
    const video = wrap.querySelector("video");
    const src = `/api/admin/preview/${encodeURIComponent(roster.slot)}/index.m3u8`;

    if (window.Hls && window.Hls.isSupported()) {
      // mediamtx's default hlsVariant is "lowLatency" (LL-HLS, 200ms
      // parts) - without this flag hls.js ignores that and falls back to
      // whole-segment buffering (~3 segments deep by convention, ~3s at
      // the default 1s segment duration). This is preview-only; it has
      // no bearing on the actual on-air switching path (RTSP/OBS).
      const hls = new window.Hls({ lowLatencyMode: true });
      hls.loadSource(src);
      hls.attachMedia(video);
      video.play().catch(() => {});
      activePreviews.set(username, hls);
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
      // Safari plays HLS natively, no hls.js needed.
      video.src = src;
      video.play().catch(() => {});
      activePreviews.set(username, true);
    } else {
      wrap.textContent = "Vorschau in diesem Browser nicht unterstützt.";
    }
  }

  function teardownPreview(username, detailsEl) {
    const active = activePreviews.get(username);
    if (active && active !== true && typeof active.destroy === "function") {
      active.destroy();
    }
    activePreviews.delete(username);
    const wrap = (detailsEl || findDjRow(username))?.querySelector(".preview-wrap");
    const btn = (detailsEl || findDjRow(username))?.querySelector(".preview-btn");
    if (wrap) {
      wrap.classList.add("hidden");
      const video = wrap.querySelector("video");
      if (video) {
        video.removeAttribute("src");
        video.load();
      }
    }
    if (btn) btn.textContent = "Vorschau anzeigen";
  }

  function teardownAllPreviews() {
    for (const username of Array.from(activePreviews.keys())) {
      teardownPreview(username);
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

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  function renderRoster(djs) {
    rosterByUsername = {};
    for (const dj of djs || []) {
      rosterByUsername[dj.username] = dj;
    }
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

      const del = document.createElement("button");
      del.className = "delete-btn";
      del.textContent = "Löschen";
      del.addEventListener("click", () => deleteDj(dj.username));

      row.appendChild(name);
      row.appendChild(pill);
      row.appendChild(slot);
      row.appendChild(spacer);
      row.appendChild(toggle);
      row.appendChild(del);

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

  async function deleteDj(username) {
    if (!window.confirm(`${username} wirklich löschen? Der Stream-Key wird ungültig; ` +
      "eine erneute Anmeldung landet wieder in der Freischaltungs-Warteschlange.")) {
      return;
    }
    rosterError = null;
    try {
      await authedFetch(`/api/djs/${encodeURIComponent(username)}`, { method: "DELETE" });
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
