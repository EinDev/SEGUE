FROM savonet/liquidsoap:v2.2.5

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-jinja2 python3-yaml \
    && rm -rf /var/lib/apt/lists/*

COPY render_config.py /app/render_config.py
COPY main.liq.j2 /app/main.liq.j2
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER liquidsoap

ENTRYPOINT ["/entrypoint.sh"]
