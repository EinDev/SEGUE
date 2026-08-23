# The official image is FROM scratch (just the static binary, no shell) so
# that it stays minimal - but our path hooks below need `sh -c` and `curl`
# to reach the api container, so we re-layer the binary + its default
# config onto a real base, per MediaMTX's own documented pattern for this
# exact situation. Pin a specific release (no `latest`), matching this
# repo's existing convention (see the old liquidsoap/Dockerfile) - check
# https://github.com/bluenviron/mediamtx/releases for a newer one before
# a fresh deploy.
FROM bluenviron/mediamtx:1.20.1 AS mediamtx

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=mediamtx /mediamtx /usr/local/bin/mediamtx
COPY mediamtx.yml /mediamtx.yml

ENTRYPOINT ["/usr/local/bin/mediamtx"]
CMD ["/mediamtx.yml"]
