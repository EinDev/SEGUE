# SEGUE

Audio-stream switcher for VRChat club events. Multiple DJs feed in at the same
time; a single output mount stays live and never drops, and switching between
DJs happens server-side without interrupting listeners. Ships as a Docker
Compose stack, built to deploy on [Coolify](https://coolify.io/) with as few
manual steps as possible.

## Prerequisites

- A Coolify instance (self-hosted, v4+) with a server it can deploy to.
- An **Authentik** instance reachable from that same Coolify/Traefik setup.
  This app has no login form of its own - every DJ and the admin both
  authenticate via an Authentik forward-auth middleware attached to the
  `api` service's Coolify domain (step 4 below). If you don't already run
  Authentik, set that up first; it's out of scope for this repo.
- On that server: ports **8000** and **8005** free and allowed through the
  firewall, in addition to whatever Coolify/Traefik already uses (80/443).
  These two ports carry DJ audio directly and must **not** go through
  Coolify's reverse proxy (see "What can go wrong" below).
- DJs need an encoder that can push to an Icecast-style harbor with a
  username *and* password (not just a password): Mixxx, VirtualDJ, or BUTT
  all support this out of the box.

## How DJ access works (no manual roster file)

There is no `config/djs.yaml` to hand-edit. Instead:

1. A DJ opens `https://<your-domain>/dj`, logs in via Authentik (however
   your Authentik setup presents that - SSO, a local account, whatever),
   and lands on their dashboard. Their harbor credentials (a mount slot and
   a random password) are generated automatically on this first visit and
   never change afterwards.
2. Until an admin approves them, their dashboard just shows "waiting for
   approval" - their encoder credentials aren't shown yet because none are
   usable yet (Liquidsoap will reject their harbor connection either way).
3. The admin (the one Authentik username in `ONAIR_ADMIN_USERNAME`) opens
   `https://<your-domain>/`, finds the DJ in the "DJ-Verwaltung" panel, and
   flips them to "bereit" (ready). This assigns them one of a fixed pool of
   `ONAIR_MAX_DJS` harbor slots.
4. The DJ reloads and now sees their real host/port/mount/password to plug
   into their encoder.

Approving/revoking a DJ never restarts Liquidsoap or touches the live
output - that's the whole point of the slot-pool design (see CONCEPT.md if
you're curious why). If every slot is already taken, approving one more DJ
fails with a clear error in the admin panel until you free one up (revoke
someone, or raise `ONAIR_MAX_DJS` and redeploy - the latter needs a
Liquidsoap restart, so do it before doors open, not mid-event).

## Setup

1. **Import as a Docker Compose resource.** In Coolify, add a new resource,
   point it at this repository, and let it detect `docker-compose.yaml`.

2. **Configure secrets.** Copy `.env.example` to `.env` and fill in real
   values - the shared internal secret, `ONAIR_ADMIN_USERNAME` (your
   Authentik username), and `ONAIR_HARBOR_PUBLIC_HOST` (the server's actual
   reachable IP or hostname, **not** the Coolify domain - see below for
   why). In Coolify this usually means pasting these as the resource's
   environment variables rather than committing a `.env` file; either way,
   `.env` is already git-ignored.

3. **Set a domain on the `api` service only, with Authentik forward-auth
   attached.** In Coolify's UI, attach your domain (Coolify issues TLS
   automatically) to the `api` service, then attach your Authentik
   forward-auth middleware to that same domain/route (a Traefik
   `forwardAuth` middleware pointing at your Authentik outpost - consult
   your Authentik/Coolify setup for the exact click-path, it varies by
   version). Confirm it's configured to inject the authenticated username
   into the `X-authentik-username` header (Authentik's own default) - or
   change `ONAIR_AUTH_USERNAME_HEADER` in `.env` to match whatever header
   your setup actually uses. Leave `liquidsoap` without a domain - it
   doesn't need one; its two ports are published directly instead.

4. **Deploy.**

5. **Open the firewall for ports 8000 and 8005.** These bypass Coolify's
   proxy on purpose, so they need to be reachable directly, both in your
   server's firewall and in Coolify's proxy/port settings if it manages one
   (confirm the exact toggle in your Coolify version's UI - this varies
   slightly between versions).

6. **Point OBS at the output mount.** Add a VLC source (or Media Source) in
   OBS pointing at `http://<your-server-ip-or-domain>:8000/live`. Use the
   plain host/IP and port 8000 here, **not** the TLS domain from step 3 -
   this mount bypasses Traefik entirely, so `https://` and the Coolify
   domain won't reach it. If you had to remap `ONAIR_OUTPUT_HOST_PORT` (see
   "What can go wrong" below), use that port instead of 8000.

7. **Send DJs the one link.** Everyone uses the same URL:
   `https://<your-domain>/dj`. There's nothing to distribute per-DJ - see
   "How DJ access works" above.

## What can go wrong

Pulled from the project's error/robustness table - these are the most
likely operator mistakes on Coolify specifically:

- **Deploy fails with `port is already allocated` (usually 8000).** Ports
  8000/8005 are published directly to the Coolify server's host network
  (see the firewall step above), so they collide with anything else on that
  server already bound to the same port - common on a shared host, since
  8000 in particular is a very popular default for other apps. Fix: in the
  resource's env vars, set `ONAIR_HARBOR_HOST_PORT` and/or
  `ONAIR_OUTPUT_HOST_PORT` (see `.env.example`) to free ports instead, then
  redeploy - no compose file edit needed. If you remap
  `ONAIR_OUTPUT_HOST_PORT`, update the OBS URL in step 7 to match; if you
  remap `ONAIR_HARBOR_HOST_PORT`, update `ONAIR_HARBOR_PUBLIC_PORT` to match
  too, since that's what gets shown to DJs.

- **DJ encoders can't connect / connect then immediately drop.** This
  usually means ports 8005 (or 8000) ended up routed through Coolify's
  Traefik proxy instead of being published directly. The Icecast `SOURCE`
  method that DJ encoders use doesn't survive a reverse proxy. Fix: make
  sure no domain/proxy rule is attached to `liquidsoap` in Coolify, and that
  8000/8005 are reachable directly against the server's IP.

- **DJ logins/credentials/ready-status disappear after a redeploy.** All of
  it lives in SQLite under `./data`. If that path isn't a real persistent
  volume - e.g. Coolify was reconfigured to use an anonymous/ephemeral
  volume, or the `./data` directory was recreated - every redeploy wipes
  the whole DJ roster and everyone has to log in and get re-approved from
  scratch. Fix: confirm `./data` (and `./filler`, `./logs`) are mapped to
  real persistent paths in Coolify's storage settings, and never delete the
  `data` directory between deploys.

- **A DJ (or the admin) gets a 401/403 they shouldn't.** Almost always means
  the Authentik forward-auth middleware isn't actually attached to the
  `api` service's route, or it's not injecting the header
  `ONAIR_AUTH_USERNAME_HEADER` expects. Check Coolify's middleware
  configuration on that domain, and that `ONAIR_ADMIN_USERNAME` in `.env`
  exactly matches the admin's real Authentik username.

- **Approving one more DJ fails with "Alle N Slots sind belegt".** All
  `ONAIR_MAX_DJS` harbor slots are currently assigned to ready DJs. Either
  revoke someone who isn't using theirs, or raise `ONAIR_MAX_DJS` in `.env`
  and redeploy (this restarts Liquidsoap, so only do it between sets, never
  while someone is live).

Other situations the system already handles for you, no action needed:

- **Liquidsoap restarts** - the api detects the dropped telnet connection,
  shows a warning, reconnects with backoff, and re-syncs from Liquidsoap
  (source of truth for connections) and SQLite (source of truth for
  mode/pin).
- **A DJ connects with the wrong password** - rejected and logged with mount
  and IP; visible in the admin event log.
- **Two DJs try to use the same mount** - the second connection is rejected
  by the harbor; visible in the log.
- **The filler folder is empty** - Liquidsoap falls back to silence
  (`blank()`), the output mount keeps running, and the admin view shows a
  warning.
- **OBS disconnects from the output mount** - Liquidsoap keeps sending;
  OBS reconnects on its own.
- **The operator's local network goes down** - outside this system's
  control. OBS should have a local backup audio file configured as a backup
  source for this scenario.
