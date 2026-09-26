# HawkSense self-hosted image: web app + API + scheduled price checks.
#   docker build -t hawksense .
#   docker run -d -p 8765:8765 -v hawksense-data:/data -e HAWKSENSE_TOKEN=change-me hawksense
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    HAWKSENSE_HOME=/data \
    HAWKSENSE_HOST=0.0.0.0 \
    HAWKSENSE_PORT=8765

WORKDIR /app
# no third-party dependencies, so no pip: the build needs no network access
COPY hawksense ./hawksense
ENV PYTHONPATH=/app
RUN printf '#!/bin/sh\nexec python -m hawksense "$@"\n' > /usr/local/bin/hawksense \
 && chmod 755 /usr/local/bin/hawksense \
 && python -m compileall -q /app/hawksense \
 && useradd --system --uid 10001 --user-group --home-dir /data --no-create-home hawksense \
 && mkdir -p /data && chown hawksense:hawksense /data

USER hawksense
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=6s --start-period=15s --retries=3 CMD ["hawksense", "healthcheck"]

ENTRYPOINT ["hawksense"]
CMD ["serve"]
