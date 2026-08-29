# SEGUE

Video-stream switcher for VRChat club events. Multiple DJs feed in their
visuals at the same time; the event operator's own OBS switches between them
with no reconnect/buffer glitch, and stays in sync with the same
AUTO/MANUAL/pin control the admin panel has always offered. Ships as a
plain Docker Compose stack, deployable on whatever host/orchestration
platform you already use (a bare server with `docker compose up -d`,
Coolify, Portainer, or anything else that runs a Compose file).

## Prerequisites

- A Docker host that can run this Compose stack, with ports **1935** and
  **8554** free and allowed through the firewall, in addition to whatever
  ports your reverse proxy already uses (typically 80/443). These two ports
  carry DJ video directly and must **not** go through any HTTP reverse
  proxy (see "What can go wrong" below).
- A reverse proxy / auth layer in front of the `api` service that meets two
  requirements (see "Auth & routing requirements" below):
  1. Every request to `api` - except anything under `/public/*` - must
     first be authenticated, with the authenticated username injected into
     a trusted request header.
  2. `/public/*` must be routed straight through to `api` with **no**
     authentication at all.
  This app has no login form of its own; it trusts that header completely
  and has no way to verify it wasn't forged, so your proxy **must**
  overwrite/strip any client-supplied copy of that header on every request
  before authenticating - see below for why this matters.
- DJs need **OBS** (or another RTMP-capable encoder producing H.264 video +
  AAC audio) - not a new requirement in practice, since DJs typically
  already run OBS for their visuals.
- The event operator needs **OBS 28+** on their own machine, with
  obs-websocket enabled, plus Python 3.10+ to run the
  [`lj-controller`](lj-controller/) script that keeps that OBS in sync with
  who's on air. See that directory's README for full setup.

## Auth & routing requirements

This app never handles credentials itself - it delegates all human
authentication to whatever sits in front of it, and only trusts one thing:
a request header (`ONAIR_AUTH_USERNAME_HEADER` in `.env`, default
`X-authentik-username`) carrying the authenticated username. Whoever's
username is in `ONAIR_ADMIN_USERNAME` is the primary admin, gets admin
access, and can never be demoted (it's a config value, not a database
row); everyone else who successfully authenticates is treated as a DJ,
unless an admin has promoted their username to admin from the "Admins"
panel in the admin UI - promoted admins have identical rights to the
primary admin, including promoting/demoting other users, and stay admins
until demoted again.

Concretely, your reverse proxy/auth layer needs to:

- **Authenticate every request to the `api` service** *except* the two
  routes under `/public/*` (`/public/api/lj/state` and `/public/ws/lj`),
  and inject the authenticated username into the header named by
  `ONAIR_AUTH_USERNAME_HEADER`. Any setup that can do forward-auth in
  front of an HTTP backend works: an Authentik forwardAuth outpost, oauth2-proxy,
  a platform-managed SSO integration, HTTP basic auth via the proxy itself,
  etc.
- **Never let a client set that header directly.** Since this app trusts
  the header unconditionally, your proxy must strip or overwrite it on
  every request before forwarding - otherwise anyone could send
  `<header>: <admin-username>` themselves and get admin access. Whatever
  auth mechanism you use, confirm it does this (forward-auth setups
  generally do, by design).
- **Exempt `/public/*` from authentication entirely**, but still route it
  to `api`. The [`lj-controller`](lj-controller/) script is not a human
  browser session - it authenticates itself with its own independent
  secret (`ONAIR_LJ_TOKEN`) on those two routes instead, and has no way to
  complete an interactive login flow if your proxy redirects it into one.
- **Terminate TLS and route `api`'s port (8080) to the outside world.**
  The `api` service only `expose`s 8080 to the Compose network by default -
  nothing publishes it to the host, so your reverse proxy (joining the
  Compose network, or reached via its own port mapping) is what makes it
  reachable at all.
- Leave `mediamtx` alone entirely - it needs no domain or auth layer at
  all; its two ports (1935/8554) are published directly instead (see
  below).

A full worked example using Authentik + Traefik (as used in this project's
own deployment) is in the "Example: Authentik + Traefik" section under
Setup below - useful as a reference even if you use something else,
since the requirements above are the same regardless of tooling.

## How DJ access works (no manual roster file)

There is no `config/djs.yaml` to hand-edit. Instead:

1. A DJ opens `https://<your-domain>/dj`, logs in via whatever your auth
   layer presents (SSO, a local account, whatever), and lands on their
   dashboard. Their publish credentials (a slot and a random password) are
   generated automatically on this first visit and never change afterwards.
2. Until an admin approves them, their dashboard just shows "waiting for
   approval" - their credentials aren't shown yet because none are usable
   yet (the relay will reject their connection either way).
3. The admin (the one username in `ONAIR_ADMIN_USERNAME`) opens
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

1. **Get the stack onto a host.** Clone this repository onto whatever
   Docker host you're deploying to, or point your platform's Docker
   Compose integration (Coolify, Portainer, etc.) at it so it picks up
   `docker-compose.yaml`.

2. **Configure secrets.** Copy `.env.example` to `.env` and fill in real
   values - the shared internal secret, `ONAIR_ADMIN_USERNAME` (the
   username your auth layer will report for the admin),
   `ONAIR_RTMP_PUBLIC_HOST`/`ONAIR_RTSP_PUBLIC_HOST` (the server's actual
   reachable IP or hostname, **not** your reverse proxy's domain - see
   below for why), `ONAIR_LJ_TOKEN`, and
   `ONAIR_LJ_READ_USERNAME`/`ONAIR_LJ_READ_PASSWORD`. If your platform
   manages environment variables itself (e.g. pasting them into a Coolify
   resource), use that instead of committing a `.env` file; either way,
   `.env` is already git-ignored.

3. **Put a reverse proxy in front of the `api` service and wire up auth.**
   `api` only `expose`s port 8080 to the Compose network by default - it's
   not published to the host - so something needs to sit in front of it,
   terminate TLS, authenticate requests, and inject the username header.
   See "Auth & routing requirements" above for exactly what's required;
   the mechanism (Traefik + Authentik, oauth2-proxy + anything, your
   platform's built-in SSO, etc.) is entirely up to you. Leave `mediamtx`
   without a domain/proxy of any kind - it doesn't need one; its two ports
   are published directly instead (step 5).

   <details>
   <summary>Example: Authentik + Traefik (this project's own deployment)</summary>

   This is one concrete way to satisfy the requirements above, using
   [Authentik](https://goauthentik.io/) as the identity provider and
   Traefik as the reverse proxy (e.g. via Coolify, which runs Traefik
   internally). Adapt it to your own Traefik config if you're not on
   Coolify.

   - **Define a forwardAuth middleware** pointed at your Authentik
     outpost, e.g. as a Traefik dynamic config file:
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
   - **Attach that middleware to the router serving the `api` service's
     domain.** On Coolify specifically, this can be done with a
     `coolify.traefik.middlewares=authentik-auth@file` label on the `api`
     service (Coolify injects it into whatever router it auto-generates
     for the domain you attach in its UI); on plain Traefik, attach the
     middleware to that router directly in your own dynamic/static config.
     This repo ships that label as a separate
     [`docker-compose.coolify.yaml`](docker-compose.coolify.yaml) file
     rather than baking it into the generic `docker-compose.yaml` (an
     earlier attempt drove it from an env var instead, but Coolify doesn't
     interpolate `${VAR:-default}` in label values the way `docker compose`
     itself does, so that never actually applied on a real deploy).
     `docker-compose.coolify.yaml` uses the Compose Specification's
     `include:` directive to pull in the base `docker-compose.yaml`, so it's
     standalone and self-sufficient - run it directly with `docker compose
     -f docker-compose.coolify.yaml up -d`, no `-f` chaining needed. On
     Coolify itself, point "Docker Compose Location" directly at
     `docker-compose.coolify.yaml`; see the comment at the top of that file
     for details.
   - Confirm the outpost is configured to inject the authenticated
     username into the `X-authentik-username` header (Authentik's own
     default, and this app's default for `ONAIR_AUTH_USERNAME_HEADER`) -
     or change `ONAIR_AUTH_USERNAME_HEADER` in `.env` to match whatever
     header your setup actually uses.
   - **Exempt `/public/*` from that same middleware** - e.g. an
     "unauthenticated paths" regex on the Provider backing the outpost, or
     a separate higher-priority Traefik router for the `/public` prefix
     with no middleware attached. Without this, the LJ controller's
     connection gets redirected to Authentik's login URL instead of
     getting a WebSocket response, and it can't connect at all.
   </details>

4. **Deploy.**

5. **Open the firewall for ports 1935 and 8554.** These bypass your
   reverse proxy on purpose, so they need to be reachable directly - in
   your server's firewall, and in your platform's own port-management
   settings if it has one (e.g. Coolify's proxy/port settings, which
   varies slightly between versions - check the exact toggle there if
   you're using it).

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
likely operator mistakes regardless of deployment platform:

- **Deploy fails with `port is already allocated` (usually 1935).** Ports
  1935/8554 are published directly to your Docker host's network (see the
  firewall step above), so they collide with anything else on that host
  already bound to the same port. Fix: set `ONAIR_RTMP_HOST_PORT` and/or
  `ONAIR_RTSP_HOST_PORT` (see `.env.example`) to free ports instead, then
  redeploy - no compose file edit needed. Update the matching
  `ONAIR_RTMP_PUBLIC_PORT`/`ONAIR_RTSP_PUBLIC_PORT` to match, since those
  are what get shown to DJs and baked into the LJ controller's URLs.

- **DJ encoders can't connect / connect then immediately drop.** This
  usually means ports 1935 (or 8554) ended up routed through an HTTP
  reverse proxy instead of being published directly - RTMP/RTSP don't
  survive a reverse proxy. Fix: make sure no domain/proxy rule is attached
  to `mediamtx`, and that 1935/8554 are reachable directly against the
  server's IP.

- **DJ logins/credentials/ready-status disappear after a redeploy.** All of
  it lives in SQLite under `./data`. If that path isn't a real persistent
  volume - e.g. your platform was reconfigured to use an anonymous/
  ephemeral volume, or the `./data` directory was recreated - every
  redeploy wipes the whole DJ roster and everyone has to log in and get
  re-approved from scratch. Fix: confirm `./data` is mapped to a real
  persistent path, and never delete it between deploys.

- **A DJ (or the admin) gets a 401/403 they shouldn't.** Almost always means
  your auth middleware isn't actually attached to the `api` service's
  route, or it's not injecting the header `ONAIR_AUTH_USERNAME_HEADER`
  expects. Check your reverse proxy's auth configuration on that domain,
  and that `ONAIR_ADMIN_USERNAME` in `.env` exactly matches the admin's
  real authenticated username.

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
  shows it getting redirected to a login page instead of connecting,
  `/public/*` isn't actually exempted from your auth layer - see "Auth &
  routing requirements" above.

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
