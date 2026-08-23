"""Host/container system metrics for the admin Diagnose panel.

Sampled once per HISTORY_SAMPLE_INTERVAL_SECONDS tick from
app.main's existing _history_collector_loop (same cadence already used for
per-slot bitrate/delay history) -- no separate loop, no separate library
beyond psutil.

Deliberately reads whatever psutil/shutil report for the container this
process runs in, not an attempt at true cgroup-limit-aware reporting (the
message.txt spec's "prefer container limits over host" is a nice-to-have,
not implemented here -- this project stays a single small container with
one process, and CPU/RAM here already reflect *this* container under
Docker's default cgroup-backed psutil behavior on Linux, which is where
this actually runs in production).

Never raises -- like mediamtx_stats.py, this feeds a best-effort dashboard
number and must not be allowed to break the collector loop or the page
that shows it.
"""
from __future__ import annotations

import logging
import shutil
import time
from typing import Optional, Tuple

import psutil

logger = logging.getLogger("segue.system_stats")

_process_start_monotonic = time.monotonic()

# (monotonic_time, cumulative_bytes) of the previous sample, for the same
# counter-to-rate conversion used for per-slot ingest bitrate.
NetPrev = Optional[Tuple[float, int]]


def sample(disk_path: str, prev_net: NetPrev) -> Tuple[dict, NetPrev]:
    """One system-stats sample, plus the updated network-counter state to
    pass into the next call. Any individual metric that fails to read
    comes back as None rather than aborting the whole sample."""
    cpu_percent = None
    memory_percent = None
    memory_used_bytes = None
    memory_total_bytes = None
    try:
        cpu_percent = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        memory_percent = vm.percent
        memory_used_bytes = vm.used
        memory_total_bytes = vm.total
    except Exception:  # noqa: BLE001
        logger.debug("cpu/memory sample failed", exc_info=True)

    disk_percent = None
    disk_used_bytes = None
    disk_total_bytes = None
    try:
        usage = shutil.disk_usage(disk_path)
        disk_total_bytes = usage.total
        disk_used_bytes = usage.used
        if usage.total:
            disk_percent = round(usage.used / usage.total * 100, 1)
    except Exception:  # noqa: BLE001
        logger.debug("disk usage sample failed for %s", disk_path, exc_info=True)

    network_mbps = None
    new_prev_net = prev_net
    try:
        counters = psutil.net_io_counters()
        counter_value = counters.bytes_sent + counters.bytes_recv
        now = time.monotonic()
        if prev_net is not None:
            prev_time, prev_value = prev_net
            elapsed = now - prev_time
            delta = counter_value - prev_value
            # A negative delta means the counter reset (interface
            # replaced/container restarted mid-run) -- same "treat as 0,
            # don't show a nonsense spike" rule as mediamtx_stats.py's
            # bitrate calculation.
            if elapsed > 0 and delta >= 0:
                network_mbps = round((delta / elapsed) / 1024 / 1024, 3)
        new_prev_net = (now, counter_value)
    except Exception:  # noqa: BLE001
        logger.debug("network counters sample failed", exc_info=True)

    stats = {
        "cpu_percent": cpu_percent,
        "memory_percent": memory_percent,
        "memory_used_bytes": memory_used_bytes,
        "memory_total_bytes": memory_total_bytes,
        "disk_percent": disk_percent,
        "disk_used_bytes": disk_used_bytes,
        "disk_total_bytes": disk_total_bytes,
        "network_mbps": network_mbps,
        "uptime_seconds": round(time.monotonic() - _process_start_monotonic),
    }
    return stats, new_prev_net
