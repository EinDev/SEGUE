FROM python:3.12-slim

WORKDIR /srv

# Build context is the repo root (see docker-compose.yaml's `api.build`),
# not this directory - needed so lj-controller/ (a repo-root sibling, not
# under api/) can be COPYed in below for /api/admin/lj/package.zip.
COPY api/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt

COPY api/app /srv/app
COPY api/static /srv/static
COPY lj-controller /srv/lj-controller

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
