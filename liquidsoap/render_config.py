#!/usr/bin/env python3
import os
import sys

from jinja2 import Environment, StrictUndefined


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: render_config.py <main.liq.j2> <out.liq>")

    template_path, out_path = sys.argv[1:3]

    max_djs = int(os.environ.get("ONAIR_MAX_DJS", "6"))
    if max_djs < 1:
        raise SystemExit("render_config: ONAIR_MAX_DJS must be at least 1")

    with open(template_path, "r", encoding="utf-8") as f:
        template_src = f.read()

    env = Environment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    template = env.from_string(template_src)

    rendered = template.render(
        max_djs=max_djs,
        telnet_port=os.environ.get("ONAIR_LIQUIDSOAP_TELNET_PORT", "1234"),
        harbor_port=os.environ.get("ONAIR_HARBOR_PORT", "8005"),
        output_port=os.environ.get("ONAIR_OUTPUT_PORT", "8000"),
        webhook_url=os.environ.get("ONAIR_WEBHOOK_URL", "http://api:8080/internal/harbor/event"),
        auth_check_url=os.environ.get("ONAIR_AUTH_CHECK_URL", "http://api:8080/internal/harbor/auth"),
        internal_secret=os.environ["ONAIR_INTERNAL_SECRET"],
    )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(rendered)


if __name__ == "__main__":
    main()
