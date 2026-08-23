# SEGUE

Video-stream switcher for VRChat club events. Multiple DJs feed in their
visuals at the same time; the event operator's own OBS switches between them
with no reconnect/buffer glitch, and stays in sync with the same
AUTO/MANUAL/pin control the admin panel has always offered. Ships as a
Docker Compose stack, built to deploy on [Coolify](https://coolify.io/) with
as few manual steps as possible.

## Prerequisites

- A Coolify instance (self-hosted, v4+) with a server it can deploy to.
- An **Authentik** instance reachable from that same Coolify/Traefik setup.
  This app has no login form of its own - every DJ and the admin both
  authenticate via an Authentik forward-auth middleware attached to the
  `api` service's Coolify domain (step 4 below). If you don't already run
  Authentik, set that up first; it's out of scope for this repo.
- On that server: ports **1935** and **8554** free and allowed through the
  firewall, in addition to whatever Coolify/Traefik already uses (80/443).
  These two ports carry DJ video directly and must **not** go through
  Coolify's reverse proxy (see "What can go wrong" below).
- DJs need **OBS** (or another RTMP-capable encoder producing H.264 video +
  AAC audio) - not a new requirement in practice, since DJs typically
  already run OBS for their visuals.
- The event operator needs **OBS 28+** on their own machine, with
  obs-websocket enabled, plus Python 3.10+ to run the
  [`lj-controller`](lj-controller/) script that keeps that OBS in sync with
  who's on air. See that directory's README for full setup.

## How DJ access works (no manual roster file)

There is no `config/djs.yaml` to hand-edit. Instead:

1. A DJ opens `https://<your-domain>/dj`, logs in via Authentik (however
   your Authentik setup presents that - SSO, a local account, whatever),
   and lands on their dashboard. Their publish credentials (a slot and a
   random password) are generated automatically on this first visit and
   never change afterwards.
2. Until an admin approves them, their dashboard just shows "waiting for
   approval" - their credentials aren't shown yet because none are usable
   yet (the relay will reject their connection either way).
3. The admin (the one Authentik username in `ONAIR_ADMIN_USERNAME`) opens
   `https://<your-domain>/`, finds the DJ in the "DJ-Verwaltung" panel, and
   flips them to "bereit" (ready). This assigns them one of a fixed pool of
   `ONAIR_MAX_DJS` slots.
4. The DJ reloads and now sees their real RTMP server + stream key to paste
   into OBS's "Custom..." stream service (Settings -> Stream -> Service:
   Custom, Server = what's shown, Stream Key = what's shown).

Approving/revoking a DJ never restarts anything or touches the live output
- that's the whole point of the slot-pool design (see CONCEPT.md if you're
curious why). If every slot is already taken, approving one more DJ fails
with a clear error in the admin panel until you free one up (revoke
someone, or raise `ONAIR_MAX_DJS` - unlike the old audio-only setup this no
longer requires restarting the media relay, only `api`, see `.env.example`).

## Setup

1. **Import as a Docker Compose resource.** In Coolify, add a new resource,
   point it at this repository, and let it detect `docker-compose.yaml`.

2. **Configure secrets.** Copy `.env.example` to `.env` and fill in real
   values - the shared internal secret, `ONAIR_ADMIN_USERNAME` (your
   Authentik username), `ONAIR_RTMP_PUBLIC_HOST`/`ONAIR_RTSP_PUBLIC_HOST`
   (the server's actual reachable IP or hostname, **not** the Coolify
   domain - see below for why), `ONAIR_LJ_TOKEN`, and
   `ONAIR_LJ_READ_USERNAME`/`ONAIR_LJ_READ_PASSWORD`. In Coolify this
   usually means pasting these as the resource's environment variables
   rather than committing a `.env` file; either way, `.env` is already
   git-ignored.

3. **Set a domain on the `api` service only, with Authentik forward-auth
   attached.** In Coolify's UI, attach your domain (Coolify issues TLS
   automatically) to the `api` service. Leave `mediamtx` without a domain -
   it doesn't need one; its two ports are published directly instead.

   Then wire up the Authentik middleware. This is a two-part setup because
   Coolify's Docker Compose resources don't expose a per-service "Network /
   Container Labels" editor the way single-Dockerfile app resources do -
   the middleware itself is defined once at the server level, and
   `docker-compose.yaml` (see the `api` service's `labels:`) just references
   it by name:
   - **Define the middleware once, server-wide**, not per-app: Coolify UI ->
     **Servers -> your server -> Proxy -> Configuration**, add a new dynamic
     config file (e.g. `authentik-auth.yaml`):
     ```yaml
     http:
       middlewares:
         authentik-auth:
           forwardAuth:
             address: 'http://<authentik-server-host>:9000/outpost.goauthentik.io/auth/traefik'
             trustForwardHeader: true
             authResponseHeaders:
               - X-authentik-username
               - X-authentik-groups
               - X-authentik-email
               - X-authentik-name
               - X-authentik-uid
     ```
   - **Attach it to the `api` service** - already done in this repo's
     `docker-compose.yaml` via the `coolify.traefik.middlewares=
     authentik-auth@file` label, which Coolify consumes at deploy time and
     injects into whatever router it auto-generates for the domain (no need
     to know/hardcode that router's Coolify-assigned name).

   Confirm the outpost is configured to inject the authenticated username
   into the `X-authentik-username` header (Authentik's own default) - or
   change `ONAIR_AUTH_USERNAME_HEADER` in `.env` to match whatever header
   your setup actually uses.

   **Exempt `/public/*` from that same middleware.** The `lj-controller`
   (see its own README) is a bare script with no browser/Authentik
   session - it authenticates with its own independent secret
   (`ONAIR_LJ_TOKEN`) on `/public/api/lj/state` and `/public/ws/lj`
   instead, and needs those two routes to bypass Authentik entirely rather
   than get redirected into an OAuth login flow it has no way to complete.
   How you configure this depends on your Authentik/Traefik setup - e.g.
   an "unauthenticated paths" regex on the Provider backing this outpost,
   or a separate higher-priority Traefik router for the `/public` prefix
   with no middleware attached. This isn't something `docker-compose.yaml`
   can do on its own; without it, the LJ controller cannot connect at all
   (confirmed: its connection gets redirected to Authentik's login URL
   instead of getting a WebSocket response).

4. **Deploy.**

5. **Open the firewall for ports 1935 and 8554.** These bypass Coolify's
   proxy on purpose, so they need to be reachable directly, both in your
   server's firewall and in Coolify's proxy/port settings if it manages one
   (confirm the exact toggle in your Coolify version's UI - this varies
   slightly between versions).

6. **Set up the LJ machine.** On the event operator's own computer: enable
   obs-websocket in OBS, create a "Live" scene with one Media Source per DJ
   slot (RTSP, pointed at that slot's URL - the LJ controller's config maps
   slot names to source names, not URLs directly) plus a standby source,
   and run the [`lj-controller`](lj-controller/) script. Full walkthrough,
   including the critical "Close file when inactive" setting, is in that
   directory's README.

7. **Send DJs the one link.** Everyone uses the same URL:
   `https://<your-domain>/dj`. There's nothing to distribute per-DJ - see
   "How DJ access works" above.

## What can go wrong

Pulled from the project's error/robustness table - these are the most
likely operator mistakes on Coolify specifically:

- **Deploy fails with `port is already allocated` (usually 1935).** Ports
  1935/8554 are published directly to the Coolify server's host network
  (see the firewall step above), so they collide with anything else on that
  server already bound to the same port. Fix: in the resource's env vars,
  set `ONAIR_RTMP_HOST_PORT` and/or `ONAIR_RTSP_HOST_PORT` (see
  `.env.example`) to free ports instead, then redeploy - no compose file
  edit needed. Update the matching `ONAIR_RTMP_PUBLIC_PORT`/
  `ONAIR_RTSP_PUBLIC_PORT` to match, since those are what get shown to DJs
  and baked into the LJ controller's URLs.

- **DJ encoders can't connect / connect then immediately drop.** This
  usually means ports 1935 (or 8554) ended up routed through Coolify's
  Traefik proxy instead of being published directly - RTMP/RTSP don't
  survive a reverse proxy. Fix: make sure no domain/proxy rule is attached
  to `mediamtx` in Coolify, and that 1935/8554 are reachable directly
  against the server's IP.

- **DJ logins/credentials/ready-status disappear after a redeploy.** All of
  it lives in SQLite under `./data`. If that path isn't a real persistent
  volume - e.g. Coolify was reconfigured to use an anonymous/ephemeral
  volume, or the `./data` directory was recreated - every redeploy wipes
  the whole DJ roster and everyone has to log in and get re-approved from
  scratch. Fix: confirm `./data` is mapped to a real persistent path in
  Coolify's storage settings, and never delete it between deploys.

- **A DJ (or the admin) gets a 401/403 they shouldn't.** Almost always means
  the Authentik forward-auth middleware isn't actually attached to the
  `api` service's route, or it's not injecting the header
  `ONAIR_AUTH_USERNAME_HEADER` expects. Check Coolify's middleware
  configuration on that domain, and that `ONAIR_ADMIN_USERNAME` in `.env`
  exactly matches the admin's real Authentik username.

- **Approving one more DJ fails with "Alle N Slots sind belegt".** All
  `ONAIR_MAX_DJS` slots are currently assigned to ready DJs. Either revoke
  someone who isn't using theirs, or raise `ONAIR_MAX_DJS` in `.env` and
  redeploy `api` (this no longer needs a media-relay restart, but still
  restarts `api` briefly, so prefer doing it between sets).

- **The LJ controller can't reach `api`.** OBS stays frozen on whoever was
  last on air instead of updating - check `lj_token` in
  `lj-controller/config.yaml` matches `ONAIR_LJ_TOKEN`, and that the LJ
  machine's network can actually reach `api_base_url` (firewall, VPN,
  whatever's between the venue and the server). If the controller's log
  shows it getting redirected to an Authentik login URL instead of
  connecting, `/public/*` isn't actually exempted from the forward-auth
  middleware on your Traefik/Authentik setup - see step 3 above.

- **The LJ controller can't reach OBS.** Same frozen-state symptom; check
  `obs_ws_url`/`obs_ws_password` in `config.yaml` against OBS's Tools ->
  WebSocket Server Settings.

- **A DJ's stream key is malformed** (missing `?user=...&pass=...`, pasted
  with a typo). Publish auth fails, visible in the admin event log the same
  way a wrong password always was - the DJ's dashboard still shows their
  correct stream key to copy again.

- **A switch to a given DJ visibly glitches/rebuffers in OBS.** Almost
  always means that DJ's Media Source still has "Close file when inactive"
  checked - see [`lj-controller/README.md`](lj-controller/README.md) for
  why this has to be unchecked on every slot's source.

Other situations the system already handles for you, no action needed:

- **mediamtx restarts** - the api detects the failed control-API poll,
  shows a warning, reconnects with backoff, and re-syncs connected DJs from
  mediamtx (source of truth for connections) and SQLite (source of truth
  for mode/pin).
- **A DJ connects with the wrong password** - rejected and logged with slot
  and IP; visible in the admin event log.
- **Two DJs try to use the same slot** - the second connection is rejected
  by mediamtx's publish auth; visible in the log.
- **Nobody is on air** - the LJ controller auto-switches to the configured
  standby source; the resolved state (`FILLER`) is exactly what it always
  was, just consumed by OBS instead of Liquidsoap's `blank()`.
- **The LJ's OBS loses its VRCDN connection** - outside this system's
  control, same as before; OBS reconnects on its own.
- **The operator's local network goes down** - outside this system's
  control. The LJ controller and OBS both degrade to "frozen on the last
  known state" rather than blanking, but a real backup plan for this
  scenario is still on the operator, same as it always was.
