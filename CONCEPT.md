# SEGUE — Implementierungs-Spezifikation

Video-Stream-Switcher für VRChat-Club-Events. Mehrere DJs speisen parallel ihre
Visuals ein, das Umschalten zwischen ihnen passiert glitchfrei im OBS des
Event-Operators (nicht mehr serverseitig - siehe Abschnitt 2/5). Deployment als
Docker-Compose-Stack in Coolify.

Dieses Dokument ist die vollständige Vorgabe für die Implementierung. Alles, was
hier nicht steht, ist bewusst offen und darf sinnvoll entschieden werden — aber die
Zustandslogik in Abschnitt 4 ist normativ und muss exakt so umgesetzt werden.

---

## 1. Problem und Ziel

Bei einem VRChat-Club-Event wechseln sich mehrere DJs mit eigenen Visuals ab. Wenn
jeder DJ einzeln zum CDN sendet, reißt der Stream bei jedem Wechsel ab. Folge: alle
Zuschauer müssen den Videoplayer neu laden, laufen in Timeouts und Rate Limits, und
bei Twitch als Quelle kommt zusätzlich Preroll-Werbung, weil jeder Reconnect als
neuer Stream gilt.

Lösung: Ein schlanker Relay-Server (MediaMTX) nimmt alle DJ-Videostreams
gleichzeitig entgegen, ohne sie zu transcodieren oder zu mischen - reines
Publish/Read-Relay. Das eigentliche Umschalten passiert nicht auf dem Server,
sondern im lokalen OBS des Event-Operators: jeder DJ-Slot ist dort eine eigene,
dauerhaft verbundene Quelle, und ein kleines Steuerskript (`lj-controller/`)
schaltet per Sichtbarkeit zwischen ihnen um, synchron zur selben
AUTO/MANUAL/Pin-Logik wie zuvor (Abschnitt 4, unverändert). OBS sendet wie schon
vorher zu VRCDN. Der Server bleibt bewusst dumm und günstig zu betreiben - alle
Rechenlast für Encoding/Switching liegt beim Operator, der die dafür nötige
Hardware ohnehin für OBS/VRCDN mitbringt.

### Nicht-Ziele

- Kein serverseitiges Compositing/Transcoding/Switching. Der Server relayt nur;
  jede Form von "Mischen" passiert im OBS des Operators.
- Kein Ersatz für OBS auf DJ- oder Operator-Seite. Beide Enden (Publish wie
  Read/VRCDN-Push) sind eigenständiges OBS, außerhalb dieses Projekts.
- Keine Musikbibliothek, kein Scheduling, keine Playlists. Der frühere
  Audio-Filler-Fallback existiert serverseitig nicht mehr - "niemand on air"
  wird stattdessen durch eine vom Operator konfigurierte Standby-Quelle in OBS
  dargestellt (siehe `lj-controller/README.md`).
- Keine Hörer-Skalierung. Genau ein Consumer pro Slot (das Operator-OBS), keine
  öffentliche Playback-Verteilung durch dieses System.

---

## 2. Systemübersicht

```
DJ 1 (OBS, RTMP-Push) ─┐
DJ 2 ───────────────────┼──► mediamtx: ein RTMP-Slot pro DJ (reines Relay,
DJ 3 ───────────────────┘     kein Transcode/Mix)
                                       │  RTSP, ein Read pro Slot,
                                       │  dauerhaft verbunden
                                       ▼
                        Lokales OBS des Operators
                        (eine Source pro Slot + Standby,
                         Umschalten = Sichtbarkeit toggeln)
                                       │
                                       ▼
                                    VRCDN
```

Steuerung und Anzeige:

```
mediamtx ── HTTP-Auth + HTTP-Webhook (connect/disconnect) ──► api (FastAPI)
                                                                 │
                            ┌────────────────────────────────────┼──────────────────┐
                            ▼                                    ▼                  ▼
                  WebSocket /public/ws/lj                 WebSocket /ws       WebSocket /ws/dj
                     lj-controller (Python,                Admin-View            DJ-Views
                     läuft beim Operator,
                     steuert OBS per
                     obs-websocket)
```

---

## 3. Services

Zwei Container im Compose-Stack, plus ein eigenständiges Skript beim Operator.

### 3.1 `mediamtx`

- Basis: offizielles MediaMTX-Image, gepinnte Release-Version (kein `latest`),
  neu geschichtet auf ein Debian-Base-Image, damit `curl` für die
  Hook-Callbacks verfügbar ist (das offizielle Image ist `FROM scratch`).
- Statische `mediamtx.yml`, **kein** Rendering/Templating beim Start nötig:
  Auth läuft live gegen `api` (`authHTTPAddress`), ein einziges
  Regex-Pfadmuster (`~^slot[0-9]+$`) deckt jede Slot-Anzahl ab.
  `ONAIR_MAX_DJS` zu erhöhen erfordert deshalb keinen Neustart dieses
  Containers mehr (Unterschied zum alten Liquidsoap-Verhalten).
- Exponiert:
  - `1935/tcp` — RTMP-Ingest für DJs (Publish)
  - `8554/tcp` — RTSP-Read für das Operator-OBS
  - `8888/tcp` — HLS, **nur im internen Compose-Netz**: liefert die
    Live-Vorschau-Thumbnails im Admin-Panel, von `api` über
    `/api/admin/preview/{slot}/...` proxied (siehe 6.1) statt direkt
    veröffentlicht - nur On-Demand aktiv, kostet also nichts, solange
    niemand eine Vorschau geöffnet hat.
  - `9997/tcp` — Control-API, **nur im internen Compose-Netz**, niemals nach
    außen (direktes Analogon zum alten Telnet-Port)
- Kein Volume für Musik/Config nötig (kein Filler-Konzept mehr serverseitig,
  siehe Abschnitt 1). Die `mediamtx.yml` selbst wird **nicht** gemountet,
  sondern vom Dockerfile ins Image gebacken - ein Bind-Mount einer
  einzelnen Datei bricht auf Coolify (siehe docker-compose.yaml's
  Kommentar dazu).

### 3.2 `api`

- Python 3.12, FastAPI, Uvicorn.
- Hält die Zustandsmaschine (Abschnitt 4, unverändert), pollt MediaMTX'
  Control-API als Reconciliation-Fallback, empfängt dessen Auth-Checks und
  Connect/Disconnect-Webhooks, bedient REST und WebSocket für drei
  Konsumenten: Admin-View, DJ-Views, und den `lj-controller` (Abschnitt 3.4).
  Anders als zuvor **kommandiert** `api` nichts mehr Richtung Medienpfad -
  MediaMTX kennt kein "on air", das Umschalten passiert ausschließlich
  clientseitig im Operator-OBS.
- SQLite unter `./data/onair.db` für persistenten Modus/Pin und das Eventlog.
- Exponiert `8080/tcp` (HTTP), wird von Coolify geproxyt.

### 3.3 `web`

Kein eigener Container nötig. Das Frontend ist statisch (Vanilla JS + CSS, kein
Build-Step) und wird von `api` unter `/static` ausgeliefert. Weniger bewegliche
Teile, weniger was am Eventabend kaputtgehen kann.

Falls doch ein Framework gewünscht ist: dann Vite-Build in ein `dist/`, das im
api-Image mitkopiert wird. Aber kein separater Node-Container zur Laufzeit.

### 3.4 `lj-controller`

- Kein Teil des Compose-Stacks - läuft eigenständig auf dem Rechner des
  Event-Operators, neben dessen OBS.
- Python-Asyncio-Skript: hält `/public/ws/lj` offen (Fallback auf
  HTTP-Polling von `/public/api/lj/state`), spiegelt den empfangenen `on_air`-Wert per
  `obs-websocket` (Sichtbarkeits-Toggle innerhalb einer festen Szene) ins
  lokale OBS.
- Details, Setup und die kritische "Close file when inactive"-Einstellung:
  siehe `lj-controller/README.md`.
- Der Admin muss dieses Verzeichnis nicht manuell aus dem Repo holen: das
  Admin-Panel bietet unter "LJ-Setup" einen Download eines fertig für
  diese Instanz vorausgefüllten Pakets (`config.yaml` mit echten Werten,
  plus Best-Effort-OBS-Szene mit einer RTSP-Quelle pro Slot) - siehe 6.1.

---

## 4. Zustandslogik (normativ)

### 4.1 Zustand

```
mode      : "AUTO" | "MANUAL"
pinned    : dj_id | null        # nur in MANUAL relevant
connected : Set[dj_id]          # von Liquidsoap gemeldet, Wahrheit
on_air    : dj_id | "FILLER"
```

### 4.2 Auflösungsregeln

Bei jedem Ereignis (Connect, Disconnect, Moduswechsel, Pinwechsel) wird `on_air`
neu bestimmt:

**MANUAL:**
1. `pinned` ist verbunden → `on_air = pinned`
2. `pinned` ist nicht verbunden → `on_air = FILLER`

Der Pin verfällt **nicht** automatisch. Wenn der gepinnte DJ rausfliegt, läuft
Filler, und sobald er wieder connectet, ist er sofort wieder on air. Das ist
Absicht: gewollte Stabilität während eines Sets, keine Überraschungssprünge.

**AUTO:**
1. Genau ein DJ verbunden → dieser geht on air
2. Mehrere verbunden und der aktuelle `on_air` ist noch dabei → bleibt unverändert
3. Mehrere verbunden und der aktuelle `on_air` ist weg → `on_air = FILLER`
   und Warnung im Interface. Nicht raten, welcher der richtige ist.
4. Keiner verbunden → `on_air = FILLER`

### 4.3 Moduswechsel

- Wechsel nach MANUAL ohne Pin: `pinned` wird auf den aktuellen `on_air` gesetzt,
  falls das ein DJ ist. Ist gerade Filler an, bleibt `pinned = null` und es läuft
  weiter Filler, bis der Betreiber jemanden pinnt.
- Wechsel nach AUTO: `pinned` wird verworfen, Regeln aus 4.2 greifen sofort.
- Ein Pin zu setzen impliziert MANUAL. Es gibt keinen Pin im AUTO-Modus.

### 4.4 Debouncing

Connect- und Disconnect-Events werden 2 Sekunden entprellt, bevor sie eine
Umschaltung auslösen. Grund: flackernde Encoder-Reconnects sollen nicht zu
Ping-Pong auf dem Ausgangsstream führen. Die Anzeige im Interface zeigt den
ungefilterten Zustand sofort, nur die Umschaltung wartet.

Ausnahme: ein Connect, der den Zustand von FILLER weg bringt, wird **nicht**
verzögert. Stille verkürzen hat Vorrang.

---

## 5. MediaMTX-Konfiguration

Statische `mediamtx.yml` (siehe `mediamtx/mediamtx.yml`), kein Templating - die
volle Begründung steht im Kommentarblock am Dateianfang dort. Kernstück:

```yaml
paths:
  "~^slot[0-9]+$":
    runOnAvailable: >
      sh -c 'curl ... /internal/mediamtx/event ... "event":"connect" ...'
    runOnUnavailable: >
      sh -c 'curl ... /internal/mediamtx/event ... "event":"disconnect" ...'
```

Wichtige Punkte:

- Kein `switch()`, kein Crossfade, kein "on air"-Konzept auf dieser Ebene mehr -
  MediaMTX relayt jeden verbundenen Slot unverändert weiter. Das serverseitige
  Analogon zu `track_sensitive=false`/dem bedingungslos-wahren letzten Zweig
  gibt es nicht mehr, weil es nichts mehr gibt, das hier umschalten könnte; die
  entsprechende Garantie ("nie leer/tot") verschiebt sich auf die
  Sichtbarkeits-Toggle-Logik im `lj-controller` (Abschnitt 3.4) plus die
  Standby-Quelle im Operator-OBS.
- `authHTTPExclude: [api]` ist notwendig, sonst würde `api`s eigenes
  Control-API-Polling (siehe 5.1) fälschlich gegen DJ/LJ-Credentials geprüft.
- Ein einziges Regex-Pfadmuster für alle Slots (kein Rendering pro
  `ONAIR_MAX_DJS`, siehe Abschnitt 3.1).

### 5.1 Control-API (Ersatz für Telnet-Kommandos)

MediaMTX hat kein Kommando-Interface wie Liquidsoaps Telnet - es gibt nichts
mehr zu kommandieren. Was bleibt, ist reines Polling zur Reconciliation:
`api` fragt periodisch `GET /v3/paths/list` auf MediaMTX' internem
Control-API (Port 9997, siehe 3.1) ab und vergleicht das Ergebnis gegen den
intern gehaltenen `connected`-Zustand - derselbe Sicherheitsnetz-Zweck, den
`onair.status` vorher hatte.

### 5.2 Auth- und Event-Hooks (Ersatz für Harbor-Callbacks)

- `authHTTPAddress` → `POST /internal/mediamtx/auth`: MediaMTX ruft dies bei
  **jedem** Publish- und Read-Versuch auf (JSON-Body mit `user`, `password`,
  `action`, `path`, ...). `action == "publish"`: Zugangsdaten müssen zu einem
  bereiten DJ **und** dessen zugewiesenem Slot passen. `action == "read"`:
  geprüft gegen das eine geteilte `ONAIR_LJ_READ_USERNAME`/`PASSWORD`-Paar.
- `runOnAvailable`/`runOnUnavailable` → `POST /internal/mediamtx/event`: feuert,
  wenn ein Slot einen lesbaren Publisher bekommt/verliert - direktes Analogon
  zu Liquidsoaps `on_connect`/`on_disconnect`. Anders als vorher trägt dieses
  Event **keinen Benutzernamen** (MediaMTX kennt DJs nicht als Konzept) - `api`
  rekonstruiert ihn aus einer beim Auth-Check befüllten In-Memory-Zuordnung
  Slot→Username, mit DB-Fallback für den Fall eines `api`-Neustarts mitten in
  einer Verbindung.

### 5.3 Eingangsformate

RTMP-Publish, H.264-Video + AAC-Audio (OBS' Standardausgabe für sein
"Benutzerdefiniert..."-Stream-Ziel). Kein Transcoding auf dem Server - was ein
DJ sendet, kommt bit-identisch beim Operator-OBS an.

---

## 6. API

### 6.1 Öffentlich (Admin, mit Session)

```
GET    /api/state                → vollständiger Zustand
POST   /api/mode      {mode}     → "AUTO" | "MANUAL"
POST   /api/pin       {dj_id}    → setzt Pin, impliziert MANUAL
POST   /api/filler               → erzwingt FILLER, setzt MANUAL, pinned=null
GET    /api/log?limit=100        → Eventlog
WS     /ws                       → Push bei jeder Zustandsänderung

GET    /api/admin/info                     → {rtmp_server} - statisch, einmalig
GET    /api/admin/stream/{username}        → Ingest-Stats (Codec/Auflösung/
                                              Bitrate/Adresse/Encoder) für einen
                                              verbundenen DJ, siehe
                                              mediamtx_stats.py
GET    /api/admin/preview/{slot}/{path...} → HLS-Proxy für die Live-Vorschau
                                              (mit LJ-Read-Credentials gegen
                                              mediamtx:8888 authentifiziert,
                                              admin-only)
GET    /api/admin/lj/package.zip           → lj-controller/ + fertig
                                              ausgefüllte config.yaml + die
                                              OBS-Szene unten, gezippt
GET    /api/admin/lj/obs-scene.json        → Best-Effort-OBS-Szenensammlung,
                                              eine RTSP-Quelle pro Slot mit
                                              echten Zugangsdaten dieser
                                              Instanz, siehe lj_package.py
                                              für den Vertrauensgrad pro
                                              Feld (Format ist von OBS nicht
                                              offiziell dokumentiert)
```

`/api/admin/stream` und die parallele DJ-eigene Variante (6.2) liefern
bewusst **keine** End-zu-Ende-Verzögerung bis VRCDN - das ist von diesem
Server aus nicht messbar, siehe `mediamtx_stats.py`s Docstring.

### 6.2 DJ-Sicht (Token in der URL, kein Login)

```
GET    /dj/{token}               → HTML-View
GET    /api/dj/{token}/state     → reduzierter Zustand
WS     /ws/dj/{token}            → Push
GET    /api/dj/me/stream         → eigene Ingest-Stats + "Verzögerung
                                    DJ → Server" (HLS-Programmzeit-Diff,
                                    siehe mediamtx_stats.py) - nur der
                                    eigene Slot, nie der anderer DJs
```

Der reduzierte Zustand enthält: eigener Status, eigene Zugangsdaten, Anzeigenamen
und Verbindungsstatus **aller** DJs, wer on air ist, aktueller Modus. Er enthält
**niemals** die Passwörter oder Tokens anderer DJs.

### 6.2a LJ-Sicht (statischer Token, kein Authentik)

```
GET    /public/api/lj/state      → Header X-Onair-Lj-Token, sonst 403
WS     /public/ws/lj             → Header X-Onair-Lj-Token, sonst 403
```

Für den `lj-controller` (Abschnitt 3.4) - kein Mensch, keine Authentik-Session,
daher ein statisches geteiltes Token (`ONAIR_LJ_TOKEN`) statt des
Proxy-Headers. **Müssen** unter `/public` liegen: die Authentik-Forward-Auth-
Middleware sitzt vor der gesamten Domain (README, Setup-Schritt 3), fängt also
auch diese Routen ab, wenn sie nicht unter dem Pfad liegen, den dieses
Deployment dafür vorsieht - ein Skript ohne Browser/Session kann den daraus resultierenden
OAuth-Redirect nicht durchlaufen. Bestätigt kaputt ohne dieses Präfix (echter
`lj-controller` erhielt einen Redirect auf die Authentik-Login-URL statt einer
WebSocket-Antwort). Antwortform wie der volle Zustand, plus pro bereitem DJ `slot`
und eine serverseitig fertig zusammengesetzte `rtsp_url`
(`rtsp://<ljread-user>:<ljread-pass>@<host>:<port>/<slot>`) - der Controller
konstruiert diese URL nie selbst.

### 6.3 Intern

```
POST   /internal/mediamtx/event  → Header X-Onair-Secret, sonst 403
POST   /internal/mediamtx/auth   → Query-Param ?secret=..., sonst 403
                                    (MediaMTX kann bei diesem Aufruf keinen
                                    eigenen Header setzen - der Secret muss
                                    deshalb Teil der in MTX_AUTHHTTPADDRESS
                                    konfigurierten URL sein, siehe
                                    docker-compose.yaml)
                                    Body: {user, password, action, path, ip, ...}
                                    200 = erlaubt, 403 = abgelehnt
```

Ersetzt die alten `/internal/harbor/*`-Endpunkte 1:1 in ihrer Rolle (siehe
Abschnitt 5.2), nur mit MediaMTX' Request-Shape statt Liquidsoaps
GET-Query-Params.

### 6.4 Zustandsobjekt

```json
{
  "mode": "MANUAL",
  "pinned": "dj2",
  "on_air": "dj2",
  "reason": "Manuell gepinnt auf Nova",
  "warning": null,
  "djs": [
    {"id": "dj1", "name": "Kite",  "connected": true,  "since": "2026-08-23T21:04:11Z"},
    {"id": "dj2", "name": "Nova",  "connected": true,  "since": "2026-08-23T22:31:02Z"},
    {"id": "dj3", "name": "Rekt",  "connected": false, "since": null}
  ],
  "server_time": "2026-08-23T22:48:30Z"
}
```

`reason` ist ein menschenlesbarer Satz, warum gerade das läuft, was läuft. Das ist
kein Luxus — um halb drei will niemand die Regeln aus Abschnitt 4 im Kopf
nachvollziehen. Beispiele: „Auto: nur Nova verbunden", „Manuell gepinnt auf Nova",
„Gepinnter DJ offline, Filler läuft".

`warning` wird gesetzt bei: gepinntem DJ offline, mehreren Verbundenen ohne
klare Auswahl im AUTO-Modus, Verbindung zu MediaMTX tot.

---

## 7. Frontend

Zwei Views, gemeinsames CSS, dunkles Theme. Wird in einem abgedunkelten Raum
neben einem Lichtpult benutzt — keine hellen Flächen, keine dünnen Schriften.

### 7.1 DJ-View

Von oben nach unten:

1. **Tally.** Der halbe Bildschirm. Drei Zustände, unverwechselbar:
   - grau, „NICHT VERBUNDEN"
   - blau, „VERBUNDEN — NICHT ON AIR"
   - rot, „ON AIR", mit dezent pulsierendem Rand
2. **Wer läuft gerade.** Name plus wie lange schon.
3. **Andere DJs.** Namensliste mit Status-Pill. Kein Detail, nur verbunden ja/nein.
4. **Deine Zugangsdaten.** RTMP-Server, Stream-Key, Format-Empfehlung.
   Copy-Button pro Feld. Stream-Key standardmäßig maskiert (er enthält
   das Passwort als Query-Param, siehe `_dj_credentials()`).
5. **Verbindungsqualität**, wenn verbunden: Auflösung, Codec, Bitrate seit
   Verbindungsaufbau, und „Verzögerung DJ → Server" (siehe 6.2/6.1 -
   bewusst nicht als Ende-zu-Ende/VRCDN-Latenz bezeichnet, weil das von
   hier aus nicht messbar ist). Alle acht Sekunden neu abgefragt, nur
   während `connected == true`.

### 7.2 Admin-View

- Kopfzeile: großer Modus-Umschalter AUTO / MANUAL, aktueller `reason`, Uhrzeit,
  RTMP-Server-Adresse (einmalig geladen, siehe `/api/admin/info`).
- Eine Zeile pro DJ: Name, Status-Pill, verbunden seit,
  „On Air schalten"-Button. Die aktive Zeile ist rot umrandet.
- Pro Zeile ein „Details"-Toggle: klappt Codec/Auflösung/Bitrate/Adresse/
  Encoder auf (siehe 6.1) sowie einen „Vorschau anzeigen"-Button, der erst
  auf Klick einen HLS-Player (vendored `hls.js`) gegen den Preview-Proxy
  startet - nie automatisch für alle verbundenen DJs gleichzeitig, um
  Bandbreite/CPU im Browser des Admins nicht unnötig zu belasten.
- Warnbanner, wenn `warning` gesetzt ist. Nicht wegklickbar, solange die
  Bedingung besteht.
- „Filler erzwingen" als deutlich abgesetzter Button.
- Eventlog, letzte 50 Einträge, mit Zeitstempel.

### 7.3 Verbindungsverhalten

WebSocket mit Exponential Backoff und Fallback auf Polling alle 3 Sekunden. Wenn
die Verbindung zur API weg ist, wird die gesamte Ansicht sichtbar ausgegraut und
mit „Verbindung zum Server verloren" überlagert. Eine eingefrorene Anzeige, die
so tut als wäre sie aktuell, ist am Eventabend schlimmer als gar keine Anzeige.

---

## 8. Konfiguration

`config/djs.yaml`:

```yaml
djs:
  - id: dj1
    name: Kite
    mount: dj1
    password: "..."       # oder ${DJ1_PASSWORD}
  - id: dj2
    name: Nova
    mount: dj2
    password: "..."
```

Environment:

```
ONAIR_ADMIN_USERNAME=        # Authentik-Username des Admins
ONAIR_AUTH_USERNAME_HEADER=X-authentik-username
ONAIR_INTERNAL_SECRET=       # Webhook/Auth mediamtx → api
ONAIR_MEDIAMTX_HOST=mediamtx
ONAIR_MEDIAMTX_API_PORT=9997
ONAIR_RTMP_PUBLIC_HOST=      # was DJs als RTMP-Server angezeigt wird
ONAIR_RTMP_PUBLIC_PORT=1935
ONAIR_RTSP_PUBLIC_HOST=      # was der lj-controller als RTSP-Quelle nutzt
ONAIR_RTSP_PUBLIC_PORT=8554
ONAIR_LJ_TOKEN=              # Shared Secret für den lj-controller
ONAIR_LJ_READ_USERNAME=      # RTSP-Lesezugangsdaten, eine geteilte Identität
ONAIR_LJ_READ_PASSWORD=
ONAIR_MAX_DJS=6
ONAIR_DEBOUNCE_SECONDS=2
ONAIR_DB_PATH=/data/onair.db
```

DJ-Tokens werden beim ersten Start generiert und in SQLite abgelegt. Ein
CLI-Kommando `onair tokens` gibt die fertigen Links zum Verschicken aus.

---

## 9. Deployment in Coolify

Als Docker-Compose-Ressource anlegen. Dinge, die hier erfahrungsgemäß
schiefgehen:

**Die RTMP/RTSP-Ports dürfen nicht durch den Reverse Proxy.** Coolify routet
HTTP über Traefik; weder RTMP noch RTSP überleben dahinter. Ports 1935 und
8554 deshalb direkt per `ports:` veröffentlichen und in der Firewall
freigeben. Nur der API-Container läuft über die Coolify-Domain mit TLS.

**Das `./data`-Volume muss persistent sein**, sonst sind nach jedem Redeploy
alle DJ-Zugangsdaten neu und der gesamte Roster ist weg:

```
./data     → SQLite
```

(Kein `./filler`/`./logs`-Volume mehr nötig - MediaMTX loggt nach stdout und
kennt kein Filler-Konzept, siehe Abschnitt 1.)

Healthchecks: `api` auf `GET /healthz`, `mediamtx` auf `GET /v3/paths/list`
gegen sein internes Control-API. `restart: unless-stopped` für beide.

---

## 10. Fehlerfälle und Robustheit

| Fall | Verhalten |
|---|---|
| mediamtx startet neu | API erkennt fehlgeschlagenes Control-API-Polling, zeigt Warnung, reconnected mit Backoff, holt Verbindungen per `/v3/paths/list` und schreibt `mode`/`pinned` aus SQLite zurück |
| API startet neu | Zustand aus SQLite laden, Verbindungen per `/v3/paths/list` von mediamtx holen. mediamtx ist die Wahrheit über Verbindungen, SQLite über Absichten |
| DJ verbindet mit falschem Passwort | Loggen mit Slot und IP, im Admin-Log sichtbar. Häufigster Supportfall am Abend |
| Zwei DJs auf demselben Slot | mediamtx lehnt den zweiten Publish ab. Im Log deutlich machen |
| Niemand on air | `on_air = FILLER` wie bisher (Abschnitt 4, unverändert); der `lj-controller` schaltet die Standby-Quelle sichtbar - kein serverseitiger Fallback mehr nötig |
| lj-controller verliert die Verbindung zu `api` oder OBS | Beide Seiten degradieren auf "eingefroren beim letzten bekannten Zustand" statt zu blanken; Reconnect mit Backoff auf beiden Verbindungen unabhängig voneinander |
| Operator-OBS trennt die Verbindung zu VRCDN | Außerhalb dieses Systems, wie zuvor. OBS reconnected von selbst |
| Netzwerk des Betreibers weg | Außerhalb des Systems. Kein automatischer Ersatz - siehe README "What can go wrong" |

---

## 11. Abnahmekriterien

Die Implementierung gilt als fertig, wenn:

1. Zwei parallel verbundene DJs möglich sind, und ein dauerhaft mitlesender
   RTSP-Consumer (z. B. `ffplay`) auf jedem Slot nie abreißt, unabhängig davon,
   wer gerade `on_air` ist - MediaMTX kennt kein "on air" und relayt beide
   Slots immer gleich.
2. Ein Wechsel im Operator-OBS (Sichtbarkeits-Toggle durch `lj-controller`)
   keinen sichtbaren Reconnect/Rebuffer erzeugt - Voraussetzung: "Close file
   when inactive" ist auf jeder Slot-Source deaktiviert (siehe
   `lj-controller/README.md`).
3. Alle Regeln aus Abschnitt 4 durch Unit-Tests der Auflösungsfunktion abgedeckt
   sind, inklusive: gepinnter DJ disconnected → Filler; gepinnter DJ reconnected →
   sofort wieder on air; AUTO mit mehreren Verbundenen springt nicht von selbst.
   (Unverändert - diese Tests laufen ohne Anpassung weiter, siehe
   `api/tests/test_state.py`.)
4. Der `lj-controller` den Sichtbarkeits-Wechsel binnen einer Sekunde nach der
   `on_air`-Änderung anwendet.
5. Ein Redeploy in Coolify weder die DJ-Zugangsdaten noch den `lj-controller`
   (unabhängiger Prozess) beeinträchtigt.

---

## 12. Reihenfolge der Umsetzung

1. MediaMTX-Config mit zwei fest verdrahteten Test-Slots. Manuell per RTMP
   einspeisen und mit einem RTSP-Player (`ffplay`) gegenprüfen, dass der
   Relay nicht abreißt. Erst wenn das steht, lohnt der Rest.
2. API-Zustandsmaschine (unverändert, Abschnitt 4) auf MediaMTX' Auth-/
   Webhook-Shape ummünzen, Tests zuerst.
3. Admin-View, DJ-View (Anpassung der Zugangsdaten-Anzeige auf RTMP-Server/
   Stream-Key).
4. `lj-controller`: erst die api-WebSocket-Anbindung, dann obs-websocket,
   zuletzt beides zusammen gegen ein echtes Test-OBS.
5. Compose, Healthchecks, Coolify.
6. Lasttest mit drei gleichzeitigen Encodern und mindestens zwanzig Wechseln
   am Stück, inklusive absichtlichem mediamtx- und api-Neustart mitten im
   Test.

---

## 13. Offene Punkte für den Betreiber

Nicht Teil der Implementierung, aber vor dem ersten Event zu klären:

- Latenzsumme messen: DJ-OBS-Encoder + MediaMTX-Relay + Operator-OBS-RTSP-Read
  + VRCDN. Relevant fürs Lichtpult, weil Licht und Ton sonst
  auseinanderlaufen - jetzt umso wichtiger, da RTMP/RTSP-Pufferverhalten sich
  vom alten MP3/Icecast-Pfad unterscheidet und dieser Mechanismus neu ist.
- Testtermin mit allen DJs auf einem separaten Test-Slot, insbesondere weil
  Stream-Key-eingebettete Zugangsdaten (`slot?user=...&pass=...`) neu sind -
  niemand sollte das erste Mal zehn Minuten vor dem Set damit hantieren.
- Standby-Inhalt für OBS festlegen (Loop-Video oder Standbild), analog zur
  früheren Filler-Entscheidung, jetzt aber als OBS-Quelle statt Playlist.