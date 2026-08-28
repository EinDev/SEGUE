// SEGUE — shared localization helper (admin + DJ views).
//
// No build step, no i18n library (see CONCEPT.md §3.3 - vanilla JS only).
// Language choice: an explicit toggle (top-right of both pages) persisted
// in localStorage; first visit falls back to the browser's language
// (German browsers keep this project's original German-only behavior,
// everyone else gets English). Switching reloads the page rather than
// trying to re-render every dynamic bit of DOM in place - simpler and
// just as fast in practice.
//
// Static markup uses data-i18n[-html] attributes applied on load; dynamic
// strings built in dj.js/admin.js/shared_chart.js call SegueI18n.t(key,
// params) directly. Keys shared across pages/files (pill states, "seit",
// etc.) live under a flat namespace so nothing has to duplicate them.

window.SegueI18n = (function () {
  "use strict";

  const STORAGE_KEY = "segue_lang";

  const de = {
    // ---- common (shared across dj/admin/shared_chart) ----
    "common.connected": "verbunden",
    "common.disconnected": "nicht verbunden",
    "common.unknown": "unbekannt",
    "common.calculating": "wird berechnet…",
    "common.notMeasurable": "nicht ermittelbar",
    "common.since": "seit {time}",
    "common.copy": "Kopieren",
    "common.copied": "Kopiert!",
    "common.copyFailed": "Fehler",
    "common.show": "Anzeigen",
    "common.hide": "Verbergen",
    "common.resolution": "Auflösung",
    "common.codec": "Codec",
    "common.bitrate": "Bitrate",
    "common.delay": "Delay DJ→Server",
    "common.connectedSince": "Verbunden",
    "common.chartBitrate5m": "Bitrate (5 Min.)",
    "common.chartDelay5m": "Verzögerung DJ→Server (5 Min.)",
    "common.noAccess": "Kein Zugriff",
    "common.connLost": "Verbindung zum Server verloren",
    "common.noData": "keine Daten",
    "common.chartCaption": "aktuell {last}{unit} · letzte 5 Min.: {range}",

    // ---- dj page ----
    "dj.title": "SEGUE — DJ",
    "dj.tally.disconnected": "NICHT VERBUNDEN",
    "dj.tally.onair": "ON AIR",
    "dj.tally.connected": "VERBUNDEN — NICHT ON AIR",
    "dj.liveNow.loading": "Lädt…",
    "dj.liveNow.prefix": "Aktuell on air: ",
    "dj.liveNow.filler": "Filler",
    "dj.liveNow.since": "— seit {m}m {s}s",
    "dj.otherDjs.title": "Andere DJs",
    "dj.otherDjs.empty": "Keine weiteren freigeschalteten DJs.",
    "dj.credentials.title": "Deine Zugangsdaten",
    "dj.credentials.server": "Server",
    "dj.credentials.streamKey": "Stream-Key",
    "dj.credentials.formatHint":
      "OBS: Einstellungen -> Stream -> Dienst 'Benutzerdefiniert...'.\n      Server und Stream-Key oben eintragen. Video H.264, Audio AAC.",
    "dj.howto.title": "Verbinden mit OBS",
    "dj.howto.intro":
      'Trag die Zugangsdaten oben (Server, Stream-Key) in OBS ein - am besten\n      per "Kopieren"-Button, um Tippfehler zu vermeiden.\n      Am wichtigsten: teste die Verbindung <em>vor</em> dem Event einmal kurz,\n      nicht erst wenn du gleich dran bist.',
    "dj.howto.obs.summary": "OBS Studio",
    "dj.howto.obs.step1": "<strong>Einstellungen</strong> → <strong>Stream</strong>.",
    "dj.howto.obs.step2": "<strong>Dienst</strong>: <em>Benutzerdefiniert...</em> auswählen.",
    "dj.howto.obs.step3":
      '<strong>Server</strong> = der Server-Wert oben, <strong>Stream-Key</strong>\n          = der Stream-Key-Wert oben (auf "Anzeigen" klicken, dann kopieren).',
    "dj.howto.obs.step4":
      "<strong>Ausgabe</strong>-Reiter: Video-Encoder H.264 (x264 oder ein\n          Hardware-Encoder), Audio-Encoder AAC - beides ist OBS' Standard, hier\n          muss meist nichts geändert werden.",
    "dj.howto.obs.step5":
      "Übernehmen, dann im Hauptfenster auf\n          <strong>Streaming starten</strong> klicken.",
    "dj.howto.note":
      'Andere Software mit generischem RTMP-Push funktioniert genauso - Server\n      und Stream-Key heißen dort oft "URL" bzw. "Key"/"Pfad". Solange sie\n      H.264-Video und AAC-Audio per RTMP senden kann, passt sie.',
    "dj.connQuality.title": "Verbindungsqualität",
    "dj.connQuality.empty": "nicht verfügbar",
    "dj.connQuality.delayHint":
      "Diese Verzögerung misst nur die Strecke von deinem Encoder bis zum\n        Server (inklusive kurzer Pufferung dort) - nicht die zusätzliche\n        Verzögerung durch das OBS des Betreibers und den Push zu VRCDN,\n        die von hier aus nicht messbar ist.",
    "dj.pending.title": "Warte auf Freischaltung",
    "dj.pending.loggedInAs": "Angemeldet als",
    "dj.pending.body":
      "Ein Admin muss dich erst freischalten, bevor du einspeisen kannst.\n    Diese Seite aktualisiert sich automatisch, sobald das passiert ist.",
    "dj.error.body":
      "Diese Seite muss über den vom Betreiber bereitgestellten Login-Weg\n    aufgerufen werden. Falls du darüber hier bist, ist etwas an der Anmeldung falsch\n    konfiguriert - bitte den Betreiber kontaktieren.",

    // ---- admin page ----
    "admin.title": "SEGUE — Admin",
    "admin.denied.body":
      "Diese Ansicht ist nur für den Admin-Account. Falls du das sein solltest, prüfe die\n    Authentik-Anmeldung und <code>ONAIR_ADMIN_USERNAME</code> auf dem Server.",
    "admin.rtmpServer.label": "RTMP-Server:",
    "admin.rtmpServer.loading": "wird geladen…",
    "admin.fillerBtn": "Filler erzwingen",
    "admin.djs.title": "DJs",
    "admin.djs.empty": "Noch keine freigeschalteten DJs.",
    "admin.djs.details": "Details",
    "admin.djs.detailsHide": "Details ausblenden",
    "admin.djs.onAirBtn": "On Air schalten",
    "admin.djs.field.remote": "Adresse",
    "admin.djs.field.agent": "Encoder",
    "admin.djs.previewShow": "Vorschau anzeigen",
    "admin.djs.previewHide": "Vorschau ausblenden",
    "admin.djs.previewUnsupported": "Vorschau in diesem Browser nicht unterstützt.",
    "admin.roster.title": "DJ-Verwaltung",
    "admin.roster.intro":
      'Jede Person, die sich einmal über den DJ-Link anmeldet, taucht hier auf.\n      Erst nach Freischaltung ("bereit") kann sie tatsächlich einspeisen.',
    "admin.roster.empty": "Noch niemand hat sich über den DJ-Link angemeldet.",
    "admin.roster.ready": "Bereit",
    "admin.roster.notReady": "Nicht bereit",
    "admin.roster.delete": "Löschen",
    "admin.roster.confirmDelete": "Wirklich löschen?",
    "admin.roster.confirmYes": "Ja",
    "admin.roster.confirmNo": "Abbrechen",
    "admin.roster.noFreeSlot": "Kein freier Slot verfügbar.",
    "admin.roster.noFreeSlotDetailed":
      "Alle {max_djs} Slots sind belegt - zuerst einen anderen DJ deaktivieren.",
    "admin.ljSetup.title": "LJ-Setup",
    "admin.ljSetup.intro":
      'Der Operator, der lokal in OBS zwischen den DJs umschaltet ("LJ"),\n      braucht ein eigenes kleines Steuerskript neben seinem OBS - siehe\n      <code>lj-controller/</code> im Repo für den vollen Hintergrund.\n      Hier gibt\'s alles fertig für diese Instanz vorbereitet zum Download.',
    "admin.ljSetup.downloadPackage": "Komplettpaket herunterladen (.zip)",
    "admin.ljSetup.downloadScene": "Nur OBS-Szene herunterladen (.json)",
    "admin.ljSetup.downloadHint":
      "Das Paket enthält bereits die Szene-Datei - der separate Button ist\n      nur praktisch, wenn schon alles andere installiert ist und nur die\n      Szene neu importiert werden soll (z. B. nach Änderung von\n      <code>ONAIR_MAX_DJS</code>).",
    "admin.ljSetup.stepsSummary": "Setup-Schritte für den LJ",
    "admin.ljSetup.step1":
      "<strong>Voraussetzungen auf dem LJ-Rechner:</strong> OBS 28\n          oder neuer (für obs-websocket, ist ab 28 eingebaut) und\n          Python 3.10+.",
    "admin.ljSetup.step2":
      "<strong>Paket entpacken</strong>, dann in einem Terminal im\n          entpackten Ordner: <code>pip install -r requirements.txt</code>.",
    "admin.ljSetup.step3":
      '<strong>OBS-Szene importieren:</strong> OBS &rarr; Szenen-Sammlung\n          &rarr; Importieren &rarr; die mitgelieferte\n          <code>segue-obs-scene.json</code> auswählen. Enthält bereits eine\n          Szene "Live" mit einer Quelle pro DJ-Slot (RTSP, mit den echten\n          Zugangsdaten dieser Instanz) plus einer Standby-Quelle.\n          <strong>Das ist ein Best-Effort-Import</strong> - OBS\'\n          Szene-Dateiformat ist nicht offiziell dokumentiert. Falls der\n          Import nicht sauber klappt, unten die manuellen Schritte\n          nutzen, das Paket enthält auch dafür die volle Anleitung\n          (<code>README.md</code>).',
    "admin.ljSetup.step4":
      '<strong>Kritisch, bei jeder der importierten Quellen\n          prüfen:</strong> Rechtsklick auf die Quelle &rarr;\n          Eigenschaften &rarr; <strong>"Close file when inactive"\n          MUSS deaktiviert sein.</strong> Steht das an, pausiert OBS die\n          Quelle im Hintergrund und jeder Wechsel dorthin verursacht ein\n          sichtbares Ruckeln/Neuverbinden - der gesamte Sinn des\n          Steuerskripts (glitchfreies Umschalten) wäre damit hinfällig.\n          Der Import setzt das zwar bereits korrekt, aber einmal von Hand\n          gegenprüfen kostet zehn Sekunden und erspart eine böse\n          Überraschung am Abend.',
    "admin.ljSetup.step5":
      "<strong>obs-websocket aktivieren:</strong> Tools &rarr;\n          WebSocket-Server-Einstellungen, aktivieren, Passwort setzen\n          (Port bleibt normalerweise 4455).",
    "admin.ljSetup.step6":
      "<strong>config.yaml prüfen</strong> (liegt im Paket bereits\n          fertig ausgefüllt, bis auf das obs-websocket-Passwort von eben -\n          das dort eintragen).",
    "admin.ljSetup.step7":
      "<strong>Starten:</strong> <code>python lj_controller.py</code>\n          in einer sichtbaren Konsole laufen lassen, für die gesamte\n          Dauer des Events - dort stehen alle Reconnects/Wechsel im Log,\n          das ist um 3 Uhr nachts die schnellste Fehlerquelle.",
    "admin.diag.title": "Diagnose",
    "admin.diag.cpu": "CPU",
    "admin.diag.ram": "RAM",
    "admin.diag.disk": "Disk",
    "admin.diag.net": "Netzwerk",
    "admin.diag.uptime": "Laufzeit",
    "admin.diag.mediamtx": "MediaMTX",
    "admin.diag.chartCpu5m": "CPU (5 Min.)",
    "admin.diag.chartRam5m": "RAM (5 Min.)",
    "admin.diag.chartNet5m": "Netzwerk (5 Min.)",
    "admin.diag.ljController": "LJ-Controller:",
    "admin.diag.errorsTitle": "Fehler & Warnungen",
    "admin.diag.errorsEmpty": "Keine Fehler oder Warnungen.",
    "admin.diag.reachable": "erreichbar",
    "admin.diag.notReachable": "nicht erreichbar",
    "admin.diag.obsConnected": "OBS verbunden",
    "admin.diag.obsDisconnected": "OBS getrennt",
    "admin.diag.lastSeen": "zuletzt gesehen {ago}",
    "admin.diag.neverSeen": "noch nie gesehen",
    "admin.diag.source": "Quelle: {source}",
    "admin.diag.agoSeconds": "vor {s}s",
    "admin.diag.agoMinutes": "vor {m}min",
    "admin.eventlog.title": "Eventlog",
    "admin.eventlog.empty": "Keine Einträge.",

    // ---- language switcher ----
    "lang.label": "Sprache",
  };

  const en = {
    // ---- common ----
    "common.connected": "connected",
    "common.disconnected": "not connected",
    "common.unknown": "unknown",
    "common.calculating": "calculating…",
    "common.notMeasurable": "not measurable",
    "common.since": "since {time}",
    "common.copy": "Copy",
    "common.copied": "Copied!",
    "common.copyFailed": "Error",
    "common.show": "Show",
    "common.hide": "Hide",
    "common.resolution": "Resolution",
    "common.codec": "Codec",
    "common.bitrate": "Bitrate",
    "common.delay": "Delay DJ→server",
    "common.connectedSince": "Connected",
    "common.chartBitrate5m": "Bitrate (5 min)",
    "common.chartDelay5m": "Delay DJ→server (5 min)",
    "common.noAccess": "No access",
    "common.connLost": "Connection to server lost",
    "common.noData": "no data",
    "common.chartCaption": "current {last}{unit} · last 5 min: {range}",

    // ---- dj page ----
    "dj.title": "SEGUE — DJ",
    "dj.tally.disconnected": "NOT CONNECTED",
    "dj.tally.onair": "ON AIR",
    "dj.tally.connected": "CONNECTED — NOT ON AIR",
    "dj.liveNow.loading": "Loading…",
    "dj.liveNow.prefix": "Currently on air: ",
    "dj.liveNow.filler": "Filler",
    "dj.liveNow.since": "— for {m}m {s}s",
    "dj.otherDjs.title": "Other DJs",
    "dj.otherDjs.empty": "No other approved DJs.",
    "dj.credentials.title": "Your credentials",
    "dj.credentials.server": "Server",
    "dj.credentials.streamKey": "Stream key",
    "dj.credentials.formatHint":
      "OBS: Settings -> Stream -> Service 'Custom...'.\n      Enter server and stream key above. Video H.264, audio AAC.",
    "dj.howto.title": "Connect with OBS",
    "dj.howto.intro":
      'Enter the credentials above (server, stream key) into OBS - best\n      via the "Copy" button, to avoid typos.\n      Most important: test the connection <em>before</em> the event once,\n      not right when it\'s your turn.',
    "dj.howto.obs.summary": "OBS Studio",
    "dj.howto.obs.step1": "<strong>Settings</strong> → <strong>Stream</strong>.",
    "dj.howto.obs.step2": "<strong>Service</strong>: select <em>Custom...</em>.",
    "dj.howto.obs.step3":
      '<strong>Server</strong> = the server value above, <strong>Stream Key</strong>\n          = the stream key value above (click "Show", then copy).',
    "dj.howto.obs.step4":
      "<strong>Output</strong> tab: video encoder H.264 (x264 or a\n          hardware encoder), audio encoder AAC - both are OBS' defaults, usually\n          nothing needs to change here.",
    "dj.howto.obs.step5":
      "Apply, then in the main window click\n          <strong>Start Streaming</strong>.",
    "dj.howto.note":
      'Other software with generic RTMP push works the same way - server\n      and stream key are often called "URL" and "Key"/"Path" there. As long as it\n      can send H.264 video and AAC audio via RTMP, it will work.',
    "dj.connQuality.title": "Connection quality",
    "dj.connQuality.empty": "not available",
    "dj.connQuality.delayHint":
      "This delay only measures the leg from your encoder to the\n        server (including brief buffering there) - not the additional\n        delay from the operator's OBS and the push to VRCDN,\n        which isn't measurable from here.",
    "dj.pending.title": "Waiting for approval",
    "dj.pending.loggedInAs": "Logged in as",
    "dj.pending.body":
      "An admin needs to approve you before you can go live.\n    This page updates automatically once that happens.",
    "dj.error.body":
      "This page must be reached via the login flow provided by the operator.\n    If that's how you got here, something in the login setup is misconfigured -\n    please contact the operator.",

    // ---- admin page ----
    "admin.title": "SEGUE — Admin",
    "admin.denied.body":
      "This view is for the admin account only. If that should be you, check\n    the Authentik login and <code>ONAIR_ADMIN_USERNAME</code> on the server.",
    "admin.rtmpServer.label": "RTMP server:",
    "admin.rtmpServer.loading": "loading…",
    "admin.fillerBtn": "Force filler",
    "admin.djs.title": "DJs",
    "admin.djs.empty": "No approved DJs yet.",
    "admin.djs.details": "Details",
    "admin.djs.detailsHide": "Hide details",
    "admin.djs.onAirBtn": "Put on air",
    "admin.djs.field.remote": "Address",
    "admin.djs.field.agent": "Encoder",
    "admin.djs.previewShow": "Show preview",
    "admin.djs.previewHide": "Hide preview",
    "admin.djs.previewUnsupported": "Preview not supported in this browser.",
    "admin.roster.title": "DJ management",
    "admin.roster.intro":
      "Anyone who has ever logged in via the DJ link shows up here.\n      They can only actually stream once approved (\"ready\").",
    "admin.roster.empty": "Nobody has logged in via the DJ link yet.",
    "admin.roster.ready": "Ready",
    "admin.roster.notReady": "Not ready",
    "admin.roster.delete": "Delete",
    "admin.roster.confirmDelete": "Really delete?",
    "admin.roster.confirmYes": "Yes",
    "admin.roster.confirmNo": "Cancel",
    "admin.roster.noFreeSlot": "No free slot available.",
    "admin.roster.noFreeSlotDetailed":
      "All {max_djs} slots are taken - deactivate another DJ first.",
    "admin.ljSetup.title": "LJ setup",
    "admin.ljSetup.intro":
      'The operator who switches locally between DJs in OBS ("LJ") needs\n      their own small control script alongside their OBS - see\n      <code>lj-controller/</code> in the repo for the full background.\n      Everything ready-made for this instance is here to download.',
    "admin.ljSetup.downloadPackage": "Download full package (.zip)",
    "admin.ljSetup.downloadScene": "Download OBS scene only (.json)",
    "admin.ljSetup.downloadHint":
      "The package already contains the scene file - the separate button is\n      only useful if everything else is already installed and only the\n      scene needs to be re-imported (e.g. after changing\n      <code>ONAIR_MAX_DJS</code>).",
    "admin.ljSetup.stepsSummary": "Setup steps for the LJ",
    "admin.ljSetup.step1":
      "<strong>Requirements on the LJ machine:</strong> OBS 28\n          or newer (obs-websocket is built in from 28 on) and\n          Python 3.10+.",
    "admin.ljSetup.step2":
      "<strong>Unzip the package</strong>, then in a terminal inside the\n          unzipped folder: <code>pip install -r requirements.txt</code>.",
    "admin.ljSetup.step3":
      '<strong>Import the OBS scene:</strong> OBS &rarr; Scene Collection\n          &rarr; Import &rarr; select the included\n          <code>segue-obs-scene.json</code>. It already contains a\n          "Live" scene with one source per DJ slot (RTSP, with this\n          instance\'s real credentials) plus a standby source.\n          <strong>This is a best-effort import</strong> - OBS\'\n          scene file format isn\'t officially documented. If the\n          import doesn\'t go cleanly, use the manual steps below\n          instead - the package also includes the full instructions\n          for that (<code>README.md</code>).',
    "admin.ljSetup.step4":
      '<strong>Critical, check for every imported\n          source:</strong> right-click the source &rarr;\n          Properties &rarr; <strong>"Close file when inactive"\n          MUST be disabled.</strong> If that\'s on, OBS pauses the\n          source in the background and every switch to it causes a\n          visible stutter/reconnect - defeating the entire point of\n          the control script (glitch-free switching). The import\n          already sets this correctly, but double-checking by hand\n          takes ten seconds and avoids a nasty surprise on the night.',
    "admin.ljSetup.step5":
      "<strong>Enable obs-websocket:</strong> Tools &rarr;\n          WebSocket Server Settings, enable it, set a password\n          (port normally stays 4455).",
    "admin.ljSetup.step6":
      "<strong>Check config.yaml</strong> (already filled in in the\n          package, except for the obs-websocket password from above -\n          enter that there).",
    "admin.ljSetup.step7":
      "<strong>Start it:</strong> run <code>python lj_controller.py</code>\n          in a visible console for the entire duration of the\n          event - all reconnects/switches show up in the log there,\n          which is the fastest way to debug something at 3am.",
    "admin.diag.title": "Diagnostics",
    "admin.diag.cpu": "CPU",
    "admin.diag.ram": "RAM",
    "admin.diag.disk": "Disk",
    "admin.diag.net": "Network",
    "admin.diag.uptime": "Uptime",
    "admin.diag.mediamtx": "MediaMTX",
    "admin.diag.chartCpu5m": "CPU (5 min)",
    "admin.diag.chartRam5m": "RAM (5 min)",
    "admin.diag.chartNet5m": "Network (5 min)",
    "admin.diag.ljController": "LJ controller:",
    "admin.diag.errorsTitle": "Errors & warnings",
    "admin.diag.errorsEmpty": "No errors or warnings.",
    "admin.diag.reachable": "reachable",
    "admin.diag.notReachable": "not reachable",
    "admin.diag.obsConnected": "OBS connected",
    "admin.diag.obsDisconnected": "OBS disconnected",
    "admin.diag.lastSeen": "last seen {ago}",
    "admin.diag.neverSeen": "never seen",
    "admin.diag.source": "Source: {source}",
    "admin.diag.agoSeconds": "{s}s ago",
    "admin.diag.agoMinutes": "{m}min ago",
    "admin.eventlog.title": "Eventlog",
    "admin.eventlog.empty": "No entries.",

    // ---- language switcher ----
    "lang.label": "Language",
  };

  const DICTS = { de: de, en: en };
  const SUPPORTED = ["de", "en"];

  function detectLang() {
    try {
      const saved = window.localStorage.getItem(STORAGE_KEY);
      if (SUPPORTED.indexOf(saved) !== -1) return saved;
    } catch (e) {
      /* localStorage unavailable (private mode etc.) - fall through */
    }
    const nav = ((navigator.language || navigator.userLanguage || "") + "").toLowerCase();
    return nav.indexOf("de") === 0 ? "de" : "en";
  }

  let lang = detectLang();

  function interpolate(str, params) {
    if (!params) return str;
    return str.replace(/\{(\w+)\}/g, (match, key) =>
      Object.prototype.hasOwnProperty.call(params, key) ? params[key] : match
    );
  }

  function t(key, params) {
    const dict = DICTS[lang] || DICTS.en;
    const str = Object.prototype.hasOwnProperty.call(dict, key) ? dict[key] : DICTS.de[key];
    if (str == null) return key;
    return interpolate(str, params);
  }

  function getLang() {
    return lang;
  }

  function setLang(newLang) {
    if (SUPPORTED.indexOf(newLang) === -1) return;
    try {
      window.localStorage.setItem(STORAGE_KEY, newLang);
    } catch (e) {
      /* non-fatal, just won't stick across reloads */
    }
    // Simplest correct thing: reload rather than re-render every dynamic
    // bit of state (tally, roster, diagnostics, ...) in place.
    window.location.reload();
  }

  // Applies data-i18n / data-i18n-html to every matching element under
  // `root` (default: whole document) and sets <html lang>. Safe to call
  // multiple times. data-i18n-html is only ever used with strings authored
  // in this file (not user input), so innerHTML is fine here.
  function applyStatic(root) {
    root = root || document;
    const textNodes = root.querySelectorAll("[data-i18n]");
    for (const el of textNodes) {
      el.textContent = t(el.getAttribute("data-i18n"));
    }
    const htmlNodes = root.querySelectorAll("[data-i18n-html]");
    for (const el of htmlNodes) {
      el.innerHTML = t(el.getAttribute("data-i18n-html"));
    }
    const titleNodes = root.querySelectorAll("[data-i18n-title]");
    for (const el of titleNodes) {
      el.setAttribute("title", t(el.getAttribute("data-i18n-title")));
    }
    document.documentElement.lang = lang;
    const titleKey = document.querySelector("title[data-i18n]");
    if (titleKey) document.title = t(titleKey.getAttribute("data-i18n"));
  }

  // Builds the small DE/EN toggle used by both pages and wires it up.
  // `mountEl`: element to append the toggle into.
  function mountSwitcher(mountEl) {
    if (!mountEl) return;
    const wrap = document.createElement("div");
    wrap.className = "lang-switch";
    wrap.setAttribute("aria-label", t("lang.label"));
    for (const code of SUPPORTED) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "lang-switch-btn" + (code === lang ? " active" : "");
      btn.textContent = code.toUpperCase();
      btn.addEventListener("click", () => {
        if (code !== lang) setLang(code);
      });
      wrap.appendChild(btn);
    }
    mountEl.appendChild(wrap);
  }

  return { t, getLang, setLang, applyStatic, mountSwitcher, detectLang };
})();
