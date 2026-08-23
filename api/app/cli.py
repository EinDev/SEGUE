"""SEGUE api CLI.

Usage:
    python -m app.cli tokens

Prints, for every configured DJ, the finished link to send out. Tokens are
generated once (on first startup of the api service, or lazily here if it
hasn't run yet) and never regenerated -- that's what keeps DJ links alive
across a redeploy per CONCEPT.md §11.5.

The base URL isn't part of CONCEPT.md's documented environment variables,
so this reads an optional ONAIR_PUBLIC_BASE_URL (e.g.
"https://segue.example.org"); if unset, it prints the bare /dj/{token} path
with a note instead of guessing a host.
"""
from __future__ import annotations

import os
import sys

from . import config as config_mod
from .db import Database


def cmd_tokens() -> int:
    djs = config_mod.load_djs()
    db_path = os.environ.get("ONAIR_DB_PATH", "/data/onair.db")
    db = Database(db_path)

    base_url = os.environ.get("ONAIR_PUBLIC_BASE_URL", "").rstrip("/")
    if not base_url:
        print(
            "(ONAIR_PUBLIC_BASE_URL not set -- printing paths only, "
            "prepend your public https:// host)",
            file=sys.stderr,
        )

    for dj in djs:
        token = db.get_or_create_token(dj.id)
        path = f"/dj/{token}"
        link = f"{base_url}{path}" if base_url else path
        print(f"{dj.name} ({dj.id}): {link}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] != "tokens":
        print("usage: python -m app.cli tokens", file=sys.stderr)
        return 2
    return cmd_tokens()


if __name__ == "__main__":
    raise SystemExit(main())
