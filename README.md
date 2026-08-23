# SEGUE

Audio-stream switcher for VRChat club events. Multiple DJs feed in at the same
time; a single output mount stays live and never drops, and switching between
DJs happens server-side without interrupting listeners. Ships as a Docker
Compose stack, built to deploy on [Coolify](https://coolify.io/) with as few
manual steps as possible.

## Prerequisites

- A Coolify instance (self-hosted, v4+) with a server it can deploy to.
- On that server: ports **8000** and **8005** free and allowed through the
  firewall, in addition to whatever Coolify/Traefik already uses (80/443).
  These two ports carry DJ audio directly and must **not** go through
  Coolify's reverse proxy (see "What can go wrong" below).
- DJs need an encoder that can push to an Icecast-style harbor: Mixxx,
  VirtualDJ, or BUTT all work out of the box.

## Setup

1. **Import as a Docker Compose resource.** In Coolify, add a new resource,
   point it at this repository, and let it detect `docker-compose.yaml`.

2. **Configure secrets.** Copy `.env.example` to `.env` and fill in real
   values - passwords, the shared internal secret, the admin token, and
   `ONAIR_HARBOR_PUBLIC_HOST` (the server's actual reachable IP or hostname,
   **not** the Coolify domain - see below for why). In Coolify this usually
   means pasting these as the resource's environment variables rather than
   committing a `.env` file; either way, `.env` is already git-ignored.

3. **List your DJs.** Copy `config/djs.yaml.example` to `config/djs.yaml` and
   add one entry per DJ (`id`, `name`, `mount`, and a `password` - either a
   literal string or `${DJ1_PASSWORD}`-style reference to a variable from
   step 2). Add a matching `DJx_PASSWORD` line to `.env` for each DJ you add
   beyond the two examples.

4. **Set a domain on the `api` service only.** In Coolify's UI, attach your
   domain (Coolify issues TLS automatically) to the `api` service. Leave
   `liquidsoap` without a domain - it doesn't need one; its two ports are
   published directly instead.

5. **Deploy.**

6. **Open the firewall for ports 8000 and 8005.** These bypass Coolify's
   proxy on purpose, so they need to be reachable directly, both in your
   server's firewall and in Coolify's proxy/port settings if it manages one
   (confirm the exact toggle in your Coolify version's UI - this varies
   slightly between versions).

7. **Point OBS at the output mount.** Add a VLC source (or Media Source) in
   OBS pointing at `http://<your-server-ip-or-domain>:8000/live`. Use the
   plain host/IP and port 8000 here, **not** the TLS domain from step 4 -
   this mount bypasses Traefik entirely, so `https://` and the Coolify
   domain won't reach it.

8. **Send DJs their links.** Each DJ gets a unique URL with their own
   credentials and a live tally view - no login required. Retrieve them with:

   ```
   docker compose exec api python -m app.cli tokens
   ```

   Set `ONAIR_PUBLIC_BASE_URL` in `.env` to your domain from step 4 (e.g.
   `https://segue.your-domain.example`) to get full clickable links; without
   it, the command prints bare `/dj/{token}` paths for you to prepend
   yourself. Tokens are generated once and persist in `./data` - rerunning
   this command later reprints the same links, it never rotates them.

## What can go wrong

Pulled from the project's error/robustness table - these two are the most
likely operator mistakes on Coolify specifically:

- **DJ encoders can't connect / connect then immediately drop.** This
  usually means ports 8005 (or 8000) ended up routed through Coolify's
  Traefik proxy instead of being published directly. The Icecast `SOURCE`
  method that DJ encoders use doesn't survive a reverse proxy. Fix: make
  sure no domain/proxy rule is attached to `liquidsoap` in Coolify, and that
  8000/8005 are reachable directly against the server's IP.

- **DJ links stop working after a redeploy.** DJ tokens live in SQLite under
  `./data`. If that path isn't a real persistent volume - e.g. Coolify was
  reconfigured to use an anonymous/ephemeral volume, or the `./data`
  directory was recreated - every redeploy wipes the tokens and all
  previously sent links go dead. Fix: confirm `./data` (and `./config`,
  `./filler`, `./logs`) are mapped to real persistent paths in Coolify's
  storage settings, and never delete the `data` directory between deploys.

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
