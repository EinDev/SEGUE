# SEGUE LJ controller

Runs on the event operator's own machine, next to the OBS instance that
already pulls SEGUE's stream and forwards it to VRCDN. It watches the
`on_air` value the `api` service resolves (same AUTO/MANUAL/pin logic as
before, see the top-level CONCEPT.md §4 - unchanged) and mirrors it into
OBS by toggling scene-item visibility, so switching between DJs' visuals
never causes a reconnect glitch.

This is operator-local tooling, not part of the Docker Compose stack - you
run it directly on the LJ machine (any OS with Python 3.10+; developed
against Windows since that's what's actually run at the venue).

## Setup

**Fastest path**: the admin panel's "LJ-Setup" card (`/` on the deployed
instance, admin login required) has a "Komplettpaket herunterladen"
button - it hands you this whole directory plus a `config.yaml` already
filled in with this deployment's real values, and a best-effort
pre-built OBS scene collection (`segue-obs-scene.json`) with one RTSP
source per DJ slot ready to import. The steps below are what that button
automates - read them anyway if the scene import doesn't come in clean,
or if you're setting this up by hand for any reason.

1. **OBS**: create one scene (its name goes in `config.yaml`'s `scene_name`,
   e.g. `"Live"`). Add one Media Source per DJ slot, each pointed at that
   slot's RTSP URL (`rtsp://<read-user>:<read-pass>@<host>:<port>/slotN` -
   the api's `/api/lj/state` response gives you these ready-made per
   connected DJ, no need to construct them by hand), plus one more source
   for a standby/filler scene (a looping video, a static "off air" image,
   whatever you want shown when nobody is on air).

   **Critical**: on every one of those Media Sources, uncheck **"Close
   file when inactive"** in its properties. If this stays checked, OBS
   stops decoding the stream while the source is hidden, and switching
   back to it forces a reconnect/buffer - visible as a glitch on every
   switch. This is the video-era equivalent of the old system's
   `track_sensitive=false` being mandatory (CONCEPT.md §5) - both exist to
   guarantee a switch is a hard cut, never a reconnect.

   Keep every one of these sources always present in the scene; the
   controller only ever toggles their visibility, it never adds/removes
   sources or rewrites a URL.

2. **obs-websocket**: OBS 28+ ships this built in. Tools -> WebSocket
   Server Settings -> enable it, set a password, note the port (default
   4455).

3. **Python deps**: `pip install -r requirements.txt` (Python 3.10+).

4. **Config**: if you got this directory from the admin panel's download,
   `config.yaml` already exists and is already correct except
   `obs_ws_password` - fill that one in and skip to step 5. Otherwise,
   copy `config.example.yaml` to `config.yaml` and fill in:
   - `api_base_url` / `lj_token` (the latter must match `ONAIR_LJ_TOKEN`
     on the server)
   - `obs_ws_url` / `obs_ws_password`
   - `scene_name`, `standby_source`
   - `slot_sources`: map every `slot1..slotN` (matching the server's
     `ONAIR_MAX_DJS`) to the OBS source name you gave it in step 1. This
     mapping is static - who's *assigned* to a given slot changes
     per-event via the admin panel, but which OBS source represents "slot3"
     does not.

5. **Run it**: `python lj_controller.py` (or pass a config path as the
   only argument). Leave it running in a visible console for the duration
   of the event - it logs every reconnect/switch, which is the fastest way
   to tell "is this actually working" at 3am.

## What it does, and doesn't, handle for you

- Reconnects to both `api` and OBS independently with exponential backoff,
  and falls back to polling `api` over plain HTTP while its WebSocket is
  down - mirrors the same resilience pattern the admin/DJ web views use.
- If OBS is unreachable when `on_air` changes, the target is remembered
  and applied the moment OBS reconnects - a restarted controller (or a
  flaky OBS websocket) self-heals instead of freezing on a stale source.
- It does **not** manage what VRCDN sees, your VRCDN push settings, or any
  scene other than the one named in `scene_name` - all of that is exactly
  the OBS setup you already had for this event.
