# SEGUE — Implementierungs-Spezifikation

Audio-Stream-Switcher für VRChat-Club-Events. Mehrere DJs speisen parallel ein,
ein einziger Ausgangsmount bleibt permanent live, Umschalten passiert serverseitig
ohne Unterbrechung. Deployment als Docker-Compose-Stack in Coolify.

Dieses Dokument ist die vollständige Vorgabe für die Implementierung. Alles, was
hier nicht steht, ist bewusst offen und darf sinnvoll entschieden werden — aber die
Zustandslogik in Abschnitt 4 ist normativ und muss exakt so umgesetzt werden.

---

## 1. Problem und Ziel

Bei einem VRChat-Club-Event wechseln sich mehrere DJs ab. Wenn jeder DJ einzeln zum
CDN sendet, reißt der Stream bei jedem Wechsel ab. Folge: alle Zuschauer müssen den
Videoplayer neu laden, laufen in Timeouts und Rate Limits, und bei Twitch als Quelle
kommt zusätzlich Preroll-Werbung, weil jeder Reconnect als neuer Stream gilt.

Lösung: Ein Server nimmt alle DJs gleichzeitig entgegen und bedient genau einen
Ausgangsmount, der nie stirbt. Der lokale OBS des Betreibers zieht diesen Mount und
sendet zu VRCDN. Der Ausgangsstream wird nie gestoppt, also muss niemand reloaden.

### Nicht-Ziele

- Kein Video. Reines Audio.
- Kein Ersatz für OBS. Der Push zu VRCDN passiert außerhalb dieses Projekts.
- Keine Musikbibliothek, kein Scheduling, keine Playlists außer dem Filler.
- Keine Hörer-Skalierung. Genau ein Consumer (OBS), plus optional ein
  Monitor-Mount mit niedriger Bitrate.

---

## 2. Systemübersicht

```
DJ 1 (Mixxx / VirtualDJ / BUTT) ─┐
DJ 2 ────────────────────────────┼──► liquidsoap: input.harbor (ein Mount pro DJ)
DJ 3 ────────────────────────────┘              │
                                                ▼
                                        switch() mit Prädikaten
                                                │
                                                ▼
                                   output.harbor /live  (MP3)
                                                │
                                                ▼
                                   Lokales OBS (VLC Source)
                                                │
                                                ▼
                                            VRCDN
```

Steuerung und Anzeige:

```
liquidsoap ◄── telnet (Kommandos) ──── api (FastAPI)
     │                                    ▲    │
     └── HTTP-Webhook (connect/disconnect)┘    │ WebSocket
                                               ▼
                                    web (Admin-View + DJ-Views)
```

---

## 3. Services

Drei Container, ein Compose-Stack.

### 3.1 `liquidsoap`

- Basis: offizielles Liquidsoap-Image, Version 2.x pinnen (kein `latest`).
- Rendert beim Start `main.liq` aus `config/djs.yaml` über ein kleines
  Python-Jinja-Skript im Entrypoint. Grund: die Anzahl der DJ-Slots und deren
  Mounts/Passwörter sind Konfiguration, nicht Code.
- Exponiert:
  - `8005/tcp` — Harbor-Ingest für DJs
  - `8000/tcp` — Ausgangsmount für OBS
  - `1234/tcp` — Telnet, **nur im internen Compose-Netz**, niemals nach außen
- Volumes: `./config` (ro), `./filler` (ro), `./logs`

### 3.2 `api`

- Python 3.12, FastAPI, Uvicorn.
- Hält die Zustandsmaschine, spricht Telnet zu Liquidsoap, empfängt die
  Harbor-Webhooks, bedient REST und WebSocket.
- SQLite unter `./data/onair.db` für persistenten Modus/Pin und das Eventlog.
- Exponiert `8080/tcp` (HTTP), wird von Coolify geproxyt.

### 3.3 `web`

Kein eigener Container nötig. Das Frontend ist statisch (Vanilla JS + CSS, kein
Build-Step) und wird von `api` unter `/static` ausgeliefert. Weniger bewegliche
Teile, weniger was am Eventabend kaputtgehen kann.

Falls doch ein Framework gewünscht ist: dann Vite-Build in ein `dist/`, das im
api-Image mitkopiert wird. Aber kein separater Node-Container zur Laufzeit.

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

## 5. Liquidsoap-Kern

Gerüst, kein fertiger Code — Funktionsnamen gegen die verwendete 2.x-Version
prüfen, zwischen 1.x und 2.x hat sich einiges verschoben.

```liquidsoap
# Zielquelle, wird per Telnet gesetzt
target = ref("FILLER")

dj1 = input.harbor("dj1", port=8005, password="...")
dj2 = input.harbor("dj2", port=8005, password="...")

filler = playlist("/filler", mode="randomize", reload_mode="watch")
safety = blank()

radio = switch(
  track_sensitive=false,
  transition_length=0.4,
  [
    ({ !target == "dj1" }, dj1),
    ({ !target == "dj2" }, dj2),
    ({ true }, fallback(track_sensitive=false, [filler, safety]))
  ]
)

output.harbor(%mp3(bitrate=320), port=8000, mount="/live", radio)
```

Wichtige Punkte:

- `track_sensitive=false` ist Pflicht, sonst wartet der Switch auf ein Trackende,
  das bei einem Live-Mount nie kommt.
- Der letzte Zweig ist bedingungslos wahr. Damit kann `radio` niemals leer laufen,
  und der Ausgangsmount stirbt nie. Das ist die zentrale Eigenschaft des ganzen
  Systems.
- `blank()` als letzter Fallback hinter dem Filler, falls das Filler-Verzeichnis
  leer oder kaputt ist.
- Übergangslänge 0.4s. Kürzer klickt, länger matscht bei Hardstyle.

### 5.1 Telnet-Kommandos

Zu registrieren:

- `onair.set <dj_id|FILLER>` — setzt `target`, gibt neuen Wert zurück
- `onair.status` — gibt JSON mit `target` und dem Ready-Status aller Harbor-Inputs
  zurück. Wird von der API beim Start und alle 5 Sekunden als Reconciliation
  abgefragt.

### 5.2 Harbor-Callbacks

`on_connect` und `on_disconnect` jedes Harbor-Inputs feuern einen HTTP-POST an
`http://api:8080/internal/harbor/event` mit `{dj_id, event, ts}` und dem
Shared Secret im Header. Der Callback darf niemals blockieren — bei einem Fehler
loggen und weitermachen, nie den Audiopfad aufhalten.

### 5.3 Eingangsformate

Harbor muss MP3, Ogg Vorbis und AAC annehmen. Mixxx sendet ohne LAME
standardmäßig Ogg Vorbis, Traktor kann historisch nur Ogg. Das darf kein
Ausschlusskriterium sein.

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
```

### 6.2 DJ-Sicht (Token in der URL, kein Login)

```
GET    /dj/{token}               → HTML-View
GET    /api/dj/{token}/state     → reduzierter Zustand
WS     /ws/dj/{token}            → Push
```

Der reduzierte Zustand enthält: eigener Status, eigene Zugangsdaten, Anzeigenamen
und Verbindungsstatus **aller** DJs, wer on air ist, aktueller Modus. Er enthält
**niemals** die Passwörter oder Tokens anderer DJs.

### 6.3 Intern

```
POST   /internal/harbor/event    → Header X-Onair-Secret, sonst 403
```

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
klare Auswahl im AUTO-Modus, Telnet-Verbindung zu Liquidsoap tot.

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
4. **Deine Zugangsdaten.** Host, Port, Mount, User, Passwort, Format-Empfehlung.
   Copy-Button pro Feld. Passwort standardmäßig maskiert.
5. **Verbindungsqualität**, wenn verfügbar: eingehende Bitrate, Dropouts der
   letzten Minuten.

### 7.2 Admin-View

- Kopfzeile: großer Modus-Umschalter AUTO / MANUAL, aktueller `reason`, Uhrzeit.
- Eine Zeile pro DJ: Name, Status-Pill, verbunden seit, Bitrate,
  „On Air schalten"-Button. Die aktive Zeile ist rot umrandet.
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
ONAIR_ADMIN_TOKEN=          # Admin-Login
ONAIR_INTERNAL_SECRET=      # Webhook Liquidsoap → API
ONAIR_LIQUIDSOAP_HOST=liquidsoap
ONAIR_LIQUIDSOAP_TELNET_PORT=1234
ONAIR_HARBOR_PUBLIC_HOST=   # was den DJs angezeigt wird
ONAIR_HARBOR_PUBLIC_PORT=8005
ONAIR_DEBOUNCE_SECONDS=2
ONAIR_DB_PATH=/data/onair.db
```

DJ-Tokens werden beim ersten Start generiert und in SQLite abgelegt. Ein
CLI-Kommando `onair tokens` gibt die fertigen Links zum Verschicken aus.

---

## 9. Deployment in Coolify

Als Docker-Compose-Ressource anlegen. Zwei Dinge, die hier erfahrungsgemäß
schiefgehen:

**Die Harbor-Ports dürfen nicht durch den Reverse Proxy.** Coolify routet HTTP
über Traefik. Das Icecast-Source-Protokoll benutzt aber teils die `SOURCE`-Methode
statt sauberem `PUT`, und daran verschlucken sich Proxies. Ports 8005 und 8000
deshalb direkt per `ports:` veröffentlichen und in der Firewall freigeben. Nur der
API-Container läuft über die Coolify-Domain mit TLS.

**Volumes müssen persistent sein**, sonst sind nach jedem Redeploy die DJ-Tokens
neu und alle verschickten Links tot:

```
./data     → SQLite
./config   → djs.yaml
./filler   → Musik für die Lücken
./logs     → Liquidsoap-Logs
```

Healthchecks: `api` auf `GET /healthz`, `liquidsoap` auf einen TCP-Connect gegen
den Telnet-Port. `restart: unless-stopped` für beide.

---

## 10. Fehlerfälle und Robustheit

| Fall | Verhalten |
|---|---|
| Liquidsoap startet neu | API erkennt Telnet-Abriss, zeigt Warnung, reconnected mit Backoff, holt Zustand per `onair.status` und schreibt `mode`/`pinned` aus SQLite zurück |
| API startet neu | Zustand aus SQLite laden, Verbindungen per `onair.status` von Liquidsoap holen. Liquidsoap ist die Wahrheit über Verbindungen, SQLite über Absichten |
| DJ verbindet mit falschem Passwort | Loggen mit Mount und IP, im Admin-Log sichtbar. Häufigster Supportfall am Abend |
| Zwei DJs auf demselben Mount | Harbor lehnt den zweiten ab. Im Log deutlich machen |
| Filler-Verzeichnis leer | `blank()` greift, Warnung im Admin-View. Der Ausgangsmount stirbt trotzdem nicht |
| OBS trennt die Verbindung zum Ausgangsmount | Nichts weiter tun. Liquidsoap sendet ins Leere, OBS reconnected von selbst |
| Netzwerk des Betreibers weg | Außerhalb des Systems. Erwähnen, dass OBS lokal eine Backup-Audiodatei als Ersatzquelle haben sollte |

---

## 11. Abnahmekriterien

Die Implementierung gilt als fertig, wenn:

1. Zwei parallel verbundene DJs möglich sind und das Umschalten zwischen ihnen im
   Ausgangsmount keine Unterbrechung erzeugt — nachweisbar dadurch, dass ein
   dauerhaft laufender Consumer den Wechsel überlebt, ohne neu zu verbinden.
2. Der Ausgangsmount über einen kompletten Testlauf inklusive Filler-Phasen und
   Liquidsoap-Neustart der DJ-Container nie abreißt.
3. Alle Regeln aus Abschnitt 4 durch Unit-Tests der Auflösungsfunktion abgedeckt
   sind, inklusive: gepinnter DJ disconnected → Filler; gepinnter DJ reconnected →
   sofort wieder on air; AUTO mit mehreren Verbundenen springt nicht von selbst.
4. Die DJ-View den Tally-Wechsel binnen einer Sekunde nach der Umschaltung anzeigt.
5. Ein Redeploy in Coolify die DJ-Links nicht invalidiert.

---

## 12. Reihenfolge der Umsetzung

1. Liquidsoap-Skript mit zwei fest verdrahteten DJs, Filler und `output.harbor`.
   Manuell per Telnet umschalten und mit einem Player gegenprüfen, dass es
   nicht abreißt. Erst wenn das steht, lohnt der Rest.
2. Config-Rendering aus `djs.yaml`.
3. API mit Zustandsmaschine, Tests zuerst.
4. Webhooks und Telnet-Anbindung.
5. Admin-View.
6. DJ-View mit Tally.
7. Compose, Volumes, Healthchecks, Coolify.
8. Lasttest mit drei gleichzeitigen Encodern und mindestens zwanzig Wechseln
   am Stück.

---

## 13. Offene Punkte für den Betreiber

Nicht Teil der Implementierung, aber vor dem ersten Event zu klären:

- Latenzsumme messen: Encoder + Harbor-Buffer + VLC-Cache in OBS + VRCDN. Relevant
  fürs Lichtpult, weil Licht und Ton sonst auseinanderlaufen.
- Testtermin mit allen DJs auf einem separaten Test-Mount. Wer rekordbox oder
  Serato fährt, braucht Virtual Audio Cable plus BUTT und sollte das nicht zehn
  Minuten vor dem Set das erste Mal sehen.
- Entscheiden, ob der Filler echte Musik oder ein Jingle-Loop sein soll.