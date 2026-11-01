# Build from the repo root: docker build -f edge/Dockerfile .
# Multi-arch: runs on the Jetson (arm64) and on a laptop (amd64) for development.
# Models run on ONNX Runtime (CPU here). On the Jetson, build FROM an L4T/JetPack base and
# install NVIDIA's onnxruntime-gpu wheel for JetPack instead, for the TensorRT and CUDA
# providers (CCTV_EDGE_MODEL_PROVIDERS picks them in order; missing ones are skipped).
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
# models: onnxruntime (detectors, classifiers); face: OpenCV's face API (attendance).
RUN pip install "./edge[models,face]"

# Fixed uid so host files (/var/lib/cctv-edge, credentials) can be owned by it.
RUN useradd --system --no-create-home --uid 10001 app && mkdir -p /data && chown app /data
USER app
VOLUME ["/data"]

# The release this image is, reported in heartbeats; set by .github/workflows/release.yml
# (empty: the package version).
ARG VERSION=""
ENV CCTV_EDGE_VERSION=${VERSION}

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD ["cctv-edge", "--healthcheck"]
CMD ["cctv-edge"]
