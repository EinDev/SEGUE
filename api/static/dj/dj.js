// SEGUE — DJ view logic.
//
// No token in the URL anymore: identity comes from the Authentik
// forward-auth proxy in front of this whole app, via a trusted request
// header the api reads server-side. This page just calls the fixed
// /api/dj/me endpoint and /ws/dj websocket -- whoever is authenticated is
// whoever it is, self-registering on first visit if they're new.

(function () {
  "use strict";

  const t = window.SegueI18n.t;
  window.SegueI18n.applyStatic();
  window.SegueI18n.mountSwitcher(document.getElementById("lang-mount"));

  const appEl = document.getElementById("app");
  const pendingAppEl = document.getElementById("pending-app");
  const errorAppEl = document.getElementById("error-app");
  const pendingUsernameEl = document.getElementById("pending-username");
  const overlayEl = document.getElementById("conn-overlay");

  const tallyEl = document.getElementById("tally");
  const tallyLabelEl = document.getElementById("tally-label");
  const tallyNameEl = document.getElementById("tally-name");
  const eventNameEl = document.getElementById("event-name");
  const liveNowEl = document.getElementById("live-now");
  const djListItemsEl = document.getElementById("dj-list-items");
  const ackBannerEl = document.getElementById("ack-banner");
  const setupDetailsEl = document.getElementById("setup-details");
  const adminLinkEl = document.getElementById("admin-link");

  let latestState = null;
  let deniedHard = false; // true only on 401 (no identity at all) -- not retried
  let setupUserToggled = false; // stop auto-collapsing once the DJ has touched it themself

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
    stopStreamStatsPolling();
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
    stopStreamStatsPolling();
  }

  function render(state) {
    const wasConnected = latestState ? latestState.dj.connected : null;
    latestState = state;
    const dj = state.dj;

    // Independent of the ready/pending gate below -- a promoted admin who
    // isn't (yet) an approved DJ themself should still be able to reach
    // the admin panel from here, not just once they're on-air-ready.
    adminLinkEl.classList.toggle("hidden", !dj.is_admin);

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
      tallyLabelEl.textContent = t("dj.tally.disconnected");
    } else if (dj.on_air === true) {
      tallyEl.classList.add("state-onair");
      tallyLabelEl.textContent = t("dj.tally.onair");
    } else {
      tallyEl.classList.add("state-connected");
      tallyLabelEl.textContent = t("dj.tally.connected");
    }
    tallyNameEl.textContent = dj.username || "";

    if (state.event_name) {
      eventNameEl.textContent = state.event_name;
      eventNameEl.classList.remove("hidden");
    } else {
      eventNameEl.classList.add("hidden");
    }

    renderLiveNow(state);
    renderDjList(state);
    renderCredentials(dj.credentials);
    renderAckBanner(state.unacked_messages);
    renderPreviewAvailability(state);

    // Stream-setup starts collapsed once the DJ is actually connected (the
    // common mid-event case: they don't need their own credentials again)
    // and open otherwise (they likely still need them) -- but only until
    // the DJ manually opens/closes it themself, see setupUserToggled.
    if (!setupUserToggled && wasConnected !== dj.connected) {
      setupDetailsEl.open = !dj.connected;
    }

    if (dj.connected) {
      startStreamStatsPolling();
    } else {
      stopStreamStatsPolling();
    }
  }

  // A click on <summary> is always a real user interaction (unlike the
  // `toggle` event, which some browsers also fire for a script-driven
  // `.open = ...` assignment) -- this is what should stop the
  // auto-collapse/expand logic above from overriding the DJ's own choice.
  const setupSummaryEl = setupDetailsEl.querySelector("summary");
  setupSummaryEl.addEventListener("click", () => {
    setupUserToggled = true;
  });

  function renderLiveNow(state) {
    const onAirUsername = state.on_air;
    const prefix = escapeHtml(t("dj.liveNow.prefix"));
    if (!onAirUsername || onAirUsername === "FILLER") {
      liveNowEl.innerHTML = `${prefix}<span class="filler">${escapeHtml(t("dj.liveNow.filler"))}</span>`;
      return;
    }
    // Duration is only computable for yourself (reduced state only carries
    // `since` for the viewer's own dj object, not for other usernames).
    let sinceIso = null;
    if (state.dj && state.dj.username === onAirUsername) {
      sinceIso = state.dj.since;
    }
    liveNowEl.innerHTML =
      `${prefix}<span class="name">${escapeHtml(onAirUsername)}</span>` +
      (sinceIso ? ` <span id="live-since-suffix"></span>` : "");
    liveNowEl.dataset.sinceIso = sinceIso || "";
  }

  function tickSince() {
    tickSchedules();
    if (!latestState) return;
    const iso = liveNowEl.dataset.sinceIso;
    const suffix = document.getElementById("live-since-suffix");
    if (!iso || !suffix) return;
    const since = new Date(iso).getTime();
    if (Number.isNaN(since)) return;
    const deltaSec = Math.max(0, Math.floor((Date.now() - since) / 1000));
    const m = Math.floor(deltaSec / 60);
    const s = deltaSec % 60;
    suffix.textContent = t("dj.liveNow.since", { m, s });
  }

  // Running order: self (marked "DU") plus every other ready DJ, each with
  // the admin-set schedule if there is one -- purely informational (see
  // db.set_schedule), rendered client-side as a live-updating "in X Min."
  // countdown by tickSchedules() below rather than a fixed timestamp.
  function renderDjList(state) {
    const selfUsername = state.dj ? state.dj.username : null;
    const djs = state.djs || [];
    djListItemsEl.innerHTML = "";
    if (djs.length === 0) {
      const li = document.createElement("li");
      li.textContent = t("dj.otherDjs.empty");
      djListItemsEl.appendChild(li);
      return;
    }
    for (const d of djs) {
      const isSelf = d.username === selfUsername;
      const li = document.createElement("li");
      li.className = isSelf ? "self" : "";

      const nameWrap = document.createElement("div");
      nameWrap.className = "dj-list-name-wrap";
      const nameSpan = document.createElement("span");
      nameSpan.textContent = d.username;
      nameWrap.appendChild(nameSpan);
      if (isSelf) {
        const tag = document.createElement("span");
        tag.className = "you-tag";
        tag.textContent = "DU";
        nameWrap.appendChild(tag);
      }

      const pill = document.createElement("span");
      pill.className = "pill " + (d.connected ? "pill-connected" : "pill-disconnected");
      pill.textContent = d.connected ? t("common.connected") : t("common.disconnected");

      li.appendChild(nameWrap);
      li.appendChild(pill);

      if (d.scheduled_start || d.scheduled_end) {
        const schedule = document.createElement("span");
        schedule.className = "schedule-text";
        schedule.dataset.start = d.scheduled_start || "";
        schedule.dataset.end = d.scheduled_end || "";
        schedule.textContent = formatSchedule(d.scheduled_start, d.scheduled_end);
        li.appendChild(schedule);
      }

      djListItemsEl.appendChild(li);
    }
  }

  // Purely informational countdown text, recomputed every tick (see
  // tickSince below, which now also drives this) -- never influences
  // on-air switching, which stays entirely connection-driven.
  function formatSchedule(startIso, endIso) {
    const now = Date.now();
    const parts = [];
    if (startIso) {
      const start = new Date(startIso).getTime();
      if (!Number.isNaN(start)) {
        if (start > now) {
          parts.push(`Live in ca. ${formatMinutes(start - now)}`);
        } else if (!endIso || new Date(endIso).getTime() > now) {
          parts.push("Sollte jetzt live sein");
        }
      }
    }
    if (endIso) {
      const end = new Date(endIso).getTime();
      if (!Number.isNaN(end)) {
        if (end > now) {
          parts.push(`Ende in ca. ${formatMinutes(end - now)}`);
        } else {
          parts.push("Sollte beendet sein");
        }
      }
    }
    return parts.join(" · ");
  }

  function formatMinutes(deltaMs) {
    const totalMin = Math.max(0, Math.round(deltaMs / 60000));
    if (totalMin < 60) return `${totalMin} Min.`;
    const h = Math.floor(totalMin / 60);
    const m = totalMin % 60;
    return `${h}h ${m}min`;
  }

  function tickSchedules() {
    for (const el of djListItemsEl.querySelectorAll(".schedule-text")) {
      el.textContent = formatSchedule(el.dataset.start || null, el.dataset.end || null);
    }
  }

  // ---- Forced-acknowledgment banner for unread admin messages (see
  // db.py's messages-table docstring). Deliberately not a modal: the rest
  // of the page stays usable, but the banner is hard to miss and pulses
  // until every message is individually acknowledged. ----

  function renderAckBanner(unackedMessages) {
    const messages = unackedMessages || [];
    if (messages.length === 0) {
      ackBannerEl.classList.add("hidden");
      ackBannerEl.innerHTML = "";
      return;
    }
    ackBannerEl.classList.remove("hidden");
    ackBannerEl.innerHTML = "";
    for (const msg of messages) {
      const item = document.createElement("div");
      item.className = "ack-item";

      const text = document.createElement("div");
      text.className = "ack-item-text";
      const label = document.createElement("span");
      label.className = "ack-item-label";
      label.textContent = "Nachricht vom Betreiber";
      text.appendChild(label);
      text.appendChild(document.createTextNode(msg.text));

      const btn = document.createElement("button");
      btn.className = "ack-item-btn";
      btn.textContent = "Verstanden";
      btn.addEventListener("click", () => ackMessage(msg.id, btn));

      item.appendChild(text);
      item.appendChild(btn);
      ackBannerEl.appendChild(item);
    }
  }

  async function ackMessage(id, btn) {
    btn.disabled = true;
    try {
      await fetch(`/api/dj/me/messages/${encodeURIComponent(id)}/ack`, {
        method: "POST",
        credentials: "same-origin",
      });
    } catch (e) {
      btn.disabled = false;
      return;
    }
    // Optimistic local update -- the next state push (triggered
    // server-side by the ack call itself) will confirm/reconcile this.
    if (latestState && latestState.unacked_messages) {
      latestState.unacked_messages = latestState.unacked_messages.filter((m) => m.id !== id);
      renderAckBanner(latestState.unacked_messages);
    }
  }

  let streamKeyRevealed = false;

  function renderCredentials(creds) {
    if (!creds) return;
    setText("cred-rtmp-server", creds.rtmp_server);
    document.getElementById("cred-rtmp-server").dataset.raw = creds.rtmp_server || "";
    document.getElementById("cred-stream-key").dataset.raw = creds.stream_key || "";
    renderStreamKeyField();
  }

  function renderStreamKeyField() {
    const el = document.getElementById("cred-stream-key");
    const raw = el.dataset.raw || "";
    el.textContent = streamKeyRevealed ? raw : "•".repeat(Math.max(5, raw.length || 5));
  }

  function setText(id, value) {
    document.getElementById(id).textContent = value != null ? String(value) : "";
  }

  // ---- Connection quality (own slot only, see mediamtx_stats.py for
  // what "Delay DJ→Server" does and doesn't measure) ----

  let streamStatsTimer = null;
  const STREAM_STATS_INTERVAL_MS = 8000;

  function startStreamStatsPolling() {
    if (streamStatsTimer) return;
    fetchStreamStats();
    streamStatsTimer = setInterval(fetchStreamStats, STREAM_STATS_INTERVAL_MS);
  }

  function stopStreamStatsPolling() {
    if (streamStatsTimer) {
      clearInterval(streamStatsTimer);
      streamStatsTimer = null;
    }
    renderStreamStats(null);
  }

  async function fetchStreamStats() {
    let resp;
    try {
      resp = await fetch("/api/dj/me/stream", { credentials: "same-origin" });
    } catch (e) {
      return;
    }
    if (!resp.ok) return;
    try {
      renderStreamStats(await resp.json());
    } catch (e) {
      // non-fatal, this card is secondary
    }
  }

  function renderStreamStats(data) {
    const emptyEl = document.getElementById("conn-quality-empty");
    const dataEl = document.getElementById("conn-quality-data");
    if (!data || !data.connected) {
      emptyEl.classList.remove("hidden");
      dataEl.classList.add("hidden");
      return;
    }
    emptyEl.classList.add("hidden");
    dataEl.classList.remove("hidden");
    setText("cq-resolution", data.resolution || t("common.unknown"));
    setText("cq-codec", [data.video_codec, data.audio_codec].filter(Boolean).join(" / ") || t("common.unknown"));
    setText("cq-bitrate", data.bitrate_kbps != null ? `${data.bitrate_kbps} kbit/s` : t("common.calculating"));
    setText("cq-since", data.connected_since ? formatConnSince(data.connected_since) : t("common.unknown"));
    setText(
      "cq-delay",
      data.delay_seconds != null ? `${data.delay_seconds.toFixed(1)} s` : t("common.notMeasurable")
    );

    // 5-min trend charts - "history" is server-side sampled/stored (see
    // api/app/main.py's _history_collector_loop), so this is whatever
    // window the api handed back, not something accumulated in this tab.
    window.SegueChart.renderSparkline(
      document.getElementById("cq-chart-bitrate"),
      document.getElementById("cq-chart-bitrate-caption"),
      window.SegueChart.toSeries(data.history, "bitrate_kbps"),
      { unit: " kbit/s", decimals: 0, colorClass: "chart-line--bitrate" }
    );
    window.SegueChart.renderSparkline(
      document.getElementById("cq-chart-delay"),
      document.getElementById("cq-chart-delay-caption"),
      window.SegueChart.toSeries(data.history, "delay_seconds"),
      { unit: " s", decimals: 2, colorClass: "chart-line--delay" }
    );
  }

  function formatConnSince(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return t("common.unknown");
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return t("common.since", { time: `${hh}:${mm}` });
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
    btn.textContent = failed ? t("common.copyFailed") : t("common.copied");
    setTimeout(() => {
      btn.textContent = original;
    }, 1000);
  }

  document.getElementById("cred-stream-key-toggle").addEventListener("click", () => {
    streamKeyRevealed = !streamKeyRevealed;
    document.getElementById("cred-stream-key-toggle").textContent = streamKeyRevealed
      ? t("common.hide")
      : t("common.show");
    renderStreamKeyField();
  });

  // ---- Live preview (opt-in only -- see /api/dj/onair-preview in
  // app.main for exactly what this shows and why it's the raw on-air slot
  // rather than a true "final mix": there is no server-visible final-mix
  // feed, the LJ's OBS reads slots directly and pushes the actual mix to
  // VRCDN outside this app entirely) ----

  const previewToggleBtn = document.getElementById("preview-toggle-btn");
  const previewWrapEl = document.getElementById("preview-wrap");
  const previewVideoEl = document.getElementById("preview-video");
  const previewUnavailableEl = document.getElementById("preview-unavailable");
  let previewActive = null; // Hls instance, `true` for native HLS, or null

  function renderPreviewAvailability(state) {
    const available = !!state.on_air && state.on_air !== "FILLER";
    if (!available && previewActive) {
      // The on-air DJ dropped out from under an active preview -- tear it
      // down rather than leave a player spinning on a now-dead source.
      stopPreview();
    }
    previewToggleBtn.disabled = !available && !previewActive;
    previewUnavailableEl.classList.toggle("hidden", available);
  }

  previewToggleBtn.addEventListener("click", () => {
    if (previewActive) {
      stopPreview();
    } else {
      startPreview();
    }
  });

  function startPreview() {
    previewWrapEl.classList.remove("hidden");
    previewToggleBtn.textContent = "Vorschau deaktivieren";
    const src = "/api/dj/onair-preview/index.m3u8";

    if (window.Hls && window.Hls.isSupported()) {
      const hls = new window.Hls({ lowLatencyMode: true });
      hls.loadSource(src);
      hls.attachMedia(previewVideoEl);
      previewVideoEl.play().catch(() => {});
      previewActive = hls;
    } else if (previewVideoEl.canPlayType("application/vnd.apple.mpegurl")) {
      previewVideoEl.src = src;
      previewVideoEl.play().catch(() => {});
      previewActive = true;
    } else {
      previewWrapEl.textContent = "Vorschau in diesem Browser nicht unterstützt.";
      previewActive = true; // still "on" so the toggle button can turn it back off
    }
  }

  function stopPreview() {
    if (previewActive && previewActive !== true && typeof previewActive.destroy === "function") {
      previewActive.destroy();
    }
    previewActive = null;
    previewWrapEl.classList.add("hidden");
    previewVideoEl.removeAttribute("src");
    previewVideoEl.load();
    previewToggleBtn.textContent = "Vorschau aktivieren";
  }

  // ---- Chat with the operator (see db.py's messages-table docstring).
  // Own-thread history, independent of the forced-ack banner above (which
  // only ever shows *unacknowledged* admin messages) -- this shows the
  // full back-and-forth, acked or not. ----

  const chatMessagesEl = document.getElementById("chat-messages");
  const chatFormEl = document.getElementById("chat-form");
  const chatInputEl = document.getElementById("chat-input");
  const chatSendBtnEl = document.getElementById("chat-send-btn");

  function renderChat(messages) {
    chatMessagesEl.innerHTML = "";
    if (!messages || messages.length === 0) {
      const empty = document.createElement("div");
      empty.id = "chat-empty";
      empty.textContent = "Noch keine Nachrichten.";
      chatMessagesEl.appendChild(empty);
      return;
    }
    for (const msg of messages) {
      const bubble = document.createElement("div");
      bubble.className = "chat-msg " + (msg.sender === "dj" ? "chat-msg-from-dj" : "chat-msg-from-admin");
      bubble.textContent = msg.text;
      const meta = document.createElement("span");
      meta.className = "chat-msg-meta";
      const who = msg.sender === "dj" ? "Du" : "Betreiber";
      const ackNote =
        msg.sender === "admin" && msg.acked_at
          ? " · gelesen"
          : "";
      meta.textContent = `${who}, ${formatChatTime(msg.created_at)}${ackNote}`;
      if (ackNote) meta.classList.add("chat-msg-ack");
      bubble.appendChild(meta);
      chatMessagesEl.appendChild(bubble);
    }
    chatMessagesEl.scrollTop = chatMessagesEl.scrollHeight;
  }

  function formatChatTime(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${hh}:${mm}`;
  }

  async function fetchChat() {
    let resp;
    try {
      resp = await fetch("/api/dj/me/messages", { credentials: "same-origin" });
    } catch (e) {
      return;
    }
    if (!resp.ok) return;
    try {
      renderChat(await resp.json());
    } catch (e) {
      // non-fatal, chat is secondary
    }
  }

  chatFormEl.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const text = chatInputEl.value.trim();
    if (!text) return;
    chatSendBtnEl.disabled = true;
    try {
      await fetch("/api/dj/me/messages", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      chatInputEl.value = "";
      await fetchChat();
    } catch (e) {
      // leave the text in the input so nothing typed is lost
    } finally {
      chatSendBtnEl.disabled = false;
    }
  });

  const CHAT_POLL_INTERVAL_MS = 6000;
  setInterval(() => {
    if (!deniedHard && latestState && latestState.dj && latestState.dj.ready) fetchChat();
  }, CHAT_POLL_INTERVAL_MS);

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
      fetchChat();
    }
  });

  setInterval(tickSince, 1000);
})();
