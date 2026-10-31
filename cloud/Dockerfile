# Build from the repo root: docker build -f cloud/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# A font with Latin and Arabic letters for PDF reports.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY contracts/ contracts/
RUN pip install ./contracts

COPY cloud/ cloud/
RUN pip install ./cloud

# alembic.ini and migrations/ live here.
WORKDIR /srv/cloud
RUN useradd --system --no-create-home app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["uvicorn", "cctv_cloud.main:app", "--host", "0.0.0.0", "--port", "8000"]
