"""Unit tests for mediamtx_stats.get_ingest_stats's parsing of MediaMTX's
/v3/paths/get and /v3/rtmpconns/list responses -- in particular the
video profile/level and audio sample-rate/channel-count fields added
alongside the existing resolution/bitrate/codec ones (see issue #3: the
original connection-quality card shipped with the codec/resolution/
bitrate/delay basics; these are the "more metrics" follow-up).

No real MediaMTX involved -- httpx.MockTransport stands in for it, so
these run offline and don't need pytest-asyncio (asyncio.run() is enough
for a single top-level await per test).
"""
import asyncio

import httpx

from app.mediamtx_stats import get_ingest_stats

PATHS_GET_URL = "http://mediamtx.test/v3/paths/get/slot1"
RTMPCONNS_URL = "http://mediamtx.test/v3/rtmpconns/list"


def _client(paths_get_json, rtmpconns_json=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL(PATHS_GET_URL):
            return httpx.Response(200, json=paths_get_json)
        if request.url == httpx.URL(RTMPCONNS_URL):
            return httpx.Response(200, json=rtmpconns_json or {"items": []})
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(coro):
    return asyncio.run(coro)


def test_get_ingest_stats_parses_video_profile_and_audio_props():
    # Field names (tracks2[].codecProps.{width,height,profile,level} for
    # H264, .{sampleRate,channelCount} for MPEG-4 Audio) verified against
    # the pinned MediaMTX v1.20.1 source, not guessed.
    paths_get = {
        "ready": True,
        "readyTime": "2026-08-29T10:00:00Z",
        "bytesReceived": 1_000_000,
        "tracks2": [
            {
                "codec": "H264",
                "codecProps": {"width": 1280, "height": 720, "profile": "High", "level": "4.1"},
            },
            {
                "codec": "MPEG-4 Audio",
                "codecProps": {"sampleRate": 48000, "channelCount": 2},
            },
        ],
    }

    async def go():
        async with _client(paths_get) as client:
            return await get_ingest_stats(client, "http://mediamtx.test", "slot1")

    stats = _run(go())

    assert stats is not None
    assert stats["resolution"] == "1280x720"
    assert stats["video_profile"] == "High 4.1"
    assert stats["audio_sample_rate"] == 48000
    assert stats["audio_channels"] == 2
    assert stats["bytes_received"] == 1_000_000


def test_get_ingest_stats_degrades_when_profile_and_audio_props_absent():
    # Opus (no sampleRate) and a codec with no codecProps at all: this
    # must degrade to None per-field, not raise or drop the whole result
    # (mediamtx_stats.py's stated "never break the page" contract).
    paths_get = {
        "ready": True,
        "readyTime": "2026-08-29T10:00:00Z",
        "bytesReceived": 0,
        "tracks2": [
            {"codec": "H264"},  # no codecProps at all
            {"codec": "Opus", "codecProps": {"channelCount": 2}},
        ],
    }

    async def go():
        async with _client(paths_get) as client:
            return await get_ingest_stats(client, "http://mediamtx.test", "slot1")

    stats = _run(go())

    assert stats is not None
    assert stats["resolution"] is None
    assert stats["video_profile"] is None
    assert stats["audio_sample_rate"] is None
    assert stats["audio_channels"] == 2


def test_get_ingest_stats_returns_none_when_not_publishing():
    async def go():
        async with _client({"ready": False}) as client:
            return await get_ingest_stats(client, "http://mediamtx.test", "slot1")

    assert _run(go()) is None
