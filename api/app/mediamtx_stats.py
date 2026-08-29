"""Per-slot connection/quality stats, sourced from MediaMTX's control API
and HLS output. Backs both the admin's per-connected-DJ detail view and
the DJ's own "Verbindungsqualitaet" card (see app.main's
/api/admin/stream/{username} and /api/dj/me/stream) -- same underlying
data, different authorization gate applied by the caller.

Deliberately does NOT claim to measure true end-to-end (glass-to-glass to
VRCDN) delay -- this server has no visibility into the LJ's OBS internals
or VRCDN's own ingest, so that number cannot honestly be computed here.
What IS computed and shown:

  - Ingest health (get_ingest_stats): codec, resolution, video
    profile/level (H264/H265 only), audio sample rate/channel count, an
    average bitrate since connect, total bytes received since connect,
    and (from MediaMTX's RTMP connection list) the publisher's remote
    address and encoder user-agent. Read straight off MediaMTX's control
    API, no probing involved. Field names verified against the pinned
    MediaMTX release's own source (internal/defs/api_path*.go) rather
    than guessed -- notably, there is no framerate field available here,
    so this deliberately does not show one.

  - "Verzoegerung DJ -> Server" (get_hls_delay_seconds): how far behind
    the live edge the relay's own HLS output currently is, derived by
    diffing wall-clock now against the *live edge estimate* of the slot's
    HLS variant playlist (see _latest_edge -- the last
    #EXT-X-PROGRAM-DATE-TIME tag plus the summed duration of the
    #EXT-X-PART entries after it, not just the segment-boundary
    timestamp alone; mediamtx's default hlsVariant is "lowLatency", which
    already emits ~200ms parts, so using them gets sub-second precision
    instead of being bound by the ~1s segment duration). This covers only
    the DJ-encoder-to-MediaMTX leg (plus MediaMTX's own HLS segmenting
    latency) -- it says nothing about the LJ's OBS encode or the push to
    VRCDN, both of which add more delay this server cannot see.

Every function here degrades to None/partial results on any failure
(MediaMTX unreachable, slot not currently publishing, malformed
response) rather than raising -- these are best-effort dashboard
numbers, never allowed to break the page that shows them.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger("segue.mediamtx_stats")

_VIDEO_CODECS = {"H264", "H265", "AV1", "VP8", "VP9"}
_PART_DURATION_RE = re.compile(r"DURATION=([0-9.]+)")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        # MediaMTX emits RFC3339 with a trailing "Z"; datetime.fromisoformat
        # only accepts "+00:00" for the pre-3.11 interpreters this image
        # might run, so normalize it by hand rather than assuming 3.11+.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def get_ingest_stats(
    client: httpx.AsyncClient, mediamtx_base_url: str, slot: str, timeout: float = 5.0
) -> Optional[dict]:
    """Codec/resolution/bitrate/remote-address for a slot's current
    publisher, or None if the slot isn't actually publishing right now.
    Two calls: /v3/paths/get/{slot} for track/byte-count info,
    /v3/rtmpconns/list for the one piece paths/get doesn't carry
    (remoteAddr/userAgent of the actual RTMP connection)."""
    try:
        resp = await client.get(f"{mediamtx_base_url}/v3/paths/get/{slot}", timeout=timeout)
        if resp.status_code != 200:
            return None
        path_data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("get_ingest_stats: paths/get failed for %s: %s", slot, exc)
        return None

    if not path_data.get("ready"):
        return None

    tracks = path_data.get("tracks2") or []
    video = next((t for t in tracks if t.get("codec") in _VIDEO_CODECS), None)
    audio = next((t for t in tracks if t.get("codec") not in _VIDEO_CODECS), None)

    resolution = None
    video_profile = None
    if video:
        props = video.get("codecProps") or {}
        w, h = props.get("width"), props.get("height")
        if w and h:
            resolution = f"{w}x{h}"
        # Only H264/H265 carry a human-readable profile+level string here
        # (see mediamtx's APIPathTrackCodecPropsH264/H265 -- AV1/VP9 expose
        # numeric profile/level/tier fields instead, not handled here to
        # avoid guessing at a presentation for those less-common encoders).
        profile, level = props.get("profile"), props.get("level")
        if isinstance(profile, str) and profile:
            video_profile = f"{profile} {level}" if level else profile

    audio_sample_rate = None
    audio_channels = None
    if audio:
        aprops = audio.get("codecProps") or {}
        audio_sample_rate = aprops.get("sampleRate")
        audio_channels = aprops.get("channelCount")

    ready_time = _parse_iso(path_data.get("readyTime"))
    bytes_received = path_data.get("bytesReceived") or 0
    bitrate_kbps = None
    if ready_time is not None:
        elapsed = (datetime.now(timezone.utc) - ready_time).total_seconds()
        if elapsed > 0.5:  # avoid a wild spike in the first fraction of a second
            bitrate_kbps = round((bytes_received * 8 / elapsed) / 1000, 1)

    remote_addr = None
    user_agent = None
    try:
        conns_resp = await client.get(f"{mediamtx_base_url}/v3/rtmpconns/list", timeout=timeout)
        if conns_resp.status_code == 200:
            for item in conns_resp.json().get("items") or []:
                if item.get("path") == slot and item.get("state") == "publish":
                    remote_addr = item.get("remoteAddr")
                    user_agent = item.get("userAgent")
                    break
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("get_ingest_stats: rtmpconns/list failed for %s: %s", slot, exc)

    return {
        "connected": True,
        "video_codec": video.get("codec") if video else None,
        "audio_codec": audio.get("codec") if audio else None,
        "resolution": resolution,
        "video_profile": video_profile,
        "audio_sample_rate": audio_sample_rate,
        "audio_channels": audio_channels,
        "bitrate_kbps": bitrate_kbps,
        # Total ingest volume since connect - cheap, honest "anything" of a
        # metric straight off path_data, distinct from the derived
        # since-connect average in bitrate_kbps above.
        "bytes_received": bytes_received,
        "connected_since": (
            ready_time.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            if ready_time
            else None
        ),
        "remote_addr": remote_addr,
        "user_agent": user_agent,
    }


def _latest_edge(variant_playlist_text: str) -> Optional[datetime]:
    """Estimate the live edge timestamp at sub-second precision.

    #EXT-X-PROGRAM-DATE-TIME only appears once per full segment (default
    1s, see hlsSegmentDuration), which is too coarse on its own - but
    mediamtx's default hlsVariant ("lowLatency") also emits #EXT-X-PART
    entries (default 200ms each) advancing the timeline within that
    segment. Sum the part durations that appear after the last PDT tag
    and add that to it, rather than using the segment-boundary timestamp
    alone. #EXT-X-PRELOAD-HINT (a part mediamtx is still producing, not
    yet fetchable) is deliberately excluded - it isn't "there" yet.
    """
    last_pdt: Optional[datetime] = None
    offset = 0.0
    for line in variant_playlist_text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            parsed = _parse_iso(line.split(":", 1)[1])
            if parsed is not None:
                last_pdt = parsed
                offset = 0.0
        elif line.startswith("#EXT-X-PART:") and last_pdt is not None:
            m = _PART_DURATION_RE.search(line)
            if m:
                offset += float(m.group(1))
        elif line.startswith("#EXT-X-PRELOAD-HINT:"):
            break  # not-yet-available part: stop, don't count it
    if last_pdt is None:
        return None
    return last_pdt + timedelta(seconds=offset)


def _first_variant_uri(master_playlist_text: str) -> Optional[str]:
    """Pull the first #EXT-X-STREAM-INF variant's URI out of an HLS master
    playlist -- the line immediately following #EXT-X-STREAM-INF, skipping
    the #EXT-X-MEDIA (audio-only) entries above it. Verified against a
    real playlist from the pinned MediaMTX image; format:

        #EXT-X-MEDIA:TYPE=AUDIO,...URI="audio2_stream.m3u8?session=..."
        #EXT-X-STREAM-INF:BANDWIDTH=...
        video1_stream.m3u8?session=...
    """
    lines = master_playlist_text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            for candidate in lines[i + 1 :]:
                candidate = candidate.strip()
                if candidate and not candidate.startswith("#"):
                    return candidate
    return None


async def get_hls_delay_seconds(
    client: httpx.AsyncClient,
    hls_base_url: str,
    slot: str,
    read_username: str,
    read_password: str,
    timeout: float = 6.0,
) -> Optional[float]:
    """How many seconds behind wall-clock "now" the slot's HLS output
    currently is, or None if it can't be determined (slot not publishing,
    HLS muxer hasn't produced a segment yet, request failed, ...). Never
    raises -- this is a best-effort dashboard number.

    Two GETs, both with HTTP Basic Auth (MediaMTX's authHTTPAddress check
    applies to HLS reads exactly like RTSP reads -- confirmed against the
    pinned image; the ?cookieCheck=1 redirect MediaMTX's HLS server does
    on the way in does not require any cookie-jar handling from us, plain
    Basic Auth on each request is sufficient as long as both requests
    happen close together, which they do here).
    """
    auth = (read_username, read_password)
    try:
        master_resp = await client.get(
            f"{hls_base_url}/{slot}/index.m3u8", auth=auth, follow_redirects=True, timeout=timeout
        )
        if master_resp.status_code != 200:
            return None
        variant_uri = _first_variant_uri(master_resp.text)
        if variant_uri is None:
            return None

        variant_resp = await client.get(
            f"{hls_base_url}/{slot}/{variant_uri}", auth=auth, follow_redirects=True, timeout=timeout
        )
        if variant_resp.status_code != 200:
            return None

        edge = _latest_edge(variant_resp.text)
        if edge is None:
            return None
        return max(0.0, (datetime.now(timezone.utc) - edge).total_seconds())
    except httpx.HTTPError as exc:
        logger.debug("get_hls_delay_seconds failed for %s: %s", slot, exc)
        return None
