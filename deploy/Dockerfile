# Web version of Alora Sweep. Build from the project root:
#   docker build -f deploy/Dockerfile -t alora-sweep .
# The image tag must match the playwright version in pyproject.toml.
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

WORKDIR /app
COPY pyproject.toml README.md ./
COPY alora_evv ./alora_evv
COPY config ./config
RUN pip install --no-cache-dir ".[web]"

# Ledger, reports, AxisCare exports and the portal session live on this volume.
ENV ALORA_EVV_DATA=/data
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "--factory", "alora_evv.web.app:create_app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
