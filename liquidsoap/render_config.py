#!/usr/bin/env python3
import os
import re
import sys

import yaml
from jinja2 import Environment, StrictUndefined

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(text):
    def replace(match):
        name = match.group(1)
        value = os.environ.get(name)
        if value is None:
            raise SystemExit(f"render_config: environment variable {name} is not set")
        return value

    return ENV_PATTERN.sub(replace, text)


def main():
    if len(sys.argv) != 4:
        raise SystemExit("usage: render_config.py <djs.yaml> <main.liq.j2> <out.liq>")

    djs_path, template_path, out_path = sys.argv[1:4]

    with open(djs_path, "r", encoding="utf-8") as f:
        raw = expand_env(f.read())
    data = yaml.safe_load(raw) or {}
    djs = data.get("djs", [])
    if not djs:
        raise SystemExit("render_config: djs.yaml defines no DJs")

    seen_ids = set()
    for dj in djs:
        for field in ("id", "name", "mount", "password"):
            if not dj.get(field):
                raise SystemExit(f"render_config: DJ entry missing required field '{field}': {dj}")
        if dj["id"] in seen_ids:
            raise SystemExit(f"render_config: duplicate DJ id '{dj['id']}'")
        seen_ids.add(dj["id"])

    with open(template_path, "r", encoding="utf-8") as f:
        template_src = f.read()

    env = Environment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    template = env.from_string(template_src)

    rendered = template.render(
        djs=djs,
        telnet_port=os.environ.get("ONAIR_LIQUIDSOAP_TELNET_PORT", "1234"),
        harbor_port=os.environ.get("ONAIR_HARBOR_PORT", "8005"),
        output_port=os.environ.get("ONAIR_OUTPUT_PORT", "8000"),
        webhook_url=os.environ.get("ONAIR_WEBHOOK_URL", "http://api:8080/internal/harbor/event"),
        internal_secret=os.environ["ONAIR_INTERNAL_SECRET"],
    )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(rendered)


if __name__ == "__main__":
    main()
