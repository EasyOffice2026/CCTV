# Build from the repo root: docker build -f edge/Dockerfile .
# Multi-arch: runs on the Jetson (arm64) and on a laptop (amd64) for development.
# The GPU (TensorRT) base image replaces this one when the person model lands in Phase 5.
FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CCTV_EDGE_DATA_DIR=/data \
    CCTV_EDGE_CREDENTIALS_FILE=/run/secrets/camera-credentials

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY contracts/ contracts/
RUN pip install ./contracts

COPY edge/ edge/
RUN pip install ./edge

# Fixed uid so host files (/var/lib/cctv-edge, credentials) can be owned by it.
RUN useradd --system --no-create-home --uid 10001 app && mkdir -p /data && chown app /data
USER app
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD ["cctv-edge", "--healthcheck"]
CMD ["cctv-edge"]
