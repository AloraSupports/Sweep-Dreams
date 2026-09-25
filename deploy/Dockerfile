# Web version of Alora Sweep. Build from the project root:
#   docker build -f deploy/Dockerfile -t alora-sweep .
# The image tag must match the playwright version in pyproject.toml.
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

WORKDIR /app
COPY pyproject.toml README.md ./
COPY alora_evv ./alora_evv
COPY config ./config
RUN pip install --no-cache-dir ".[web]"

# The package is installed non-editable, so point the app at the config folder
# copied above (otherwise the CLI inside the container can't find settings.yaml).
ENV ALORA_EVV_CONFIG=/app/config
# Sign-in can't be switched off on a server, whatever settings.yaml says.
ENV ALORA_EVV_WEB_AUTH=google
# Ledger timestamps and the dashboard use local time: Nebraska is Central.
ENV TZ=America/Chicago

# Ledger, reports, AxisCare exports and the portal session live on this volume.
# The folders exist from the start so the AxisCare reports can be copied in
# before the first sweep.
ENV ALORA_EVV_DATA=/data
RUN mkdir -p /data/axiscare /data/reports
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "--factory", "alora_evv.web.app:create_app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
