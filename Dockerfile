# HawkDrop self-hosted image: web app + API + scheduled price checks.
#   docker build -t hawkdrop .
#   docker run -d -p 8765:8765 -v hawkdrop-data:/data -e HAWKDROP_TOKEN=change-me hawkdrop
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    HAWKDROP_HOME=/data \
    HAWKDROP_HOST=0.0.0.0 \
    HAWKDROP_PORT=8765

WORKDIR /app
# no third-party dependencies, so no pip: the build needs no network access
COPY hawkdrop ./hawkdrop
ENV PYTHONPATH=/app
RUN printf '#!/bin/sh\nexec python -m hawkdrop "$@"\n' > /usr/local/bin/hawkdrop \
 && chmod 755 /usr/local/bin/hawkdrop \
 && python -m compileall -q /app/hawkdrop \
 && useradd --system --uid 10001 --user-group --home-dir /data --no-create-home hawkdrop \
 && mkdir -p /data && chown hawkdrop:hawkdrop /data

USER hawkdrop
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=6s --start-period=15s --retries=3 CMD ["hawkdrop", "healthcheck"]

ENTRYPOINT ["hawkdrop"]
CMD ["serve"]
