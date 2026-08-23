#!/bin/sh
set -e

python3 /app/render_config.py /app/main.liq.j2 /tmp/main.liq
exec liquidsoap /tmp/main.liq
