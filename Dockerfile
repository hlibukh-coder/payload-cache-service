# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Lives on a volume so the cache outlives the container (see docker-compose.yml).
    DATABASE_URL=sqlite:////data/payload_cache.db

WORKDIR /app

# Dependencies first, with only the metadata the build backend needs, so editing source
# code does not invalidate this (slow) layer. The PostgreSQL driver is included so the same
# image works with either backend; DATABASE_URL picks one.
COPY pyproject.toml README.md ./
COPY src/payload_cache/__init__.py src/payload_cache/__init__.py
RUN pip install ".[postgres]"

COPY src ./src
RUN pip install --no-deps .

RUN useradd --system --uid 1000 --create-home app \
    && mkdir /data \
    && chown app:app /data
USER app

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]

CMD ["uvicorn", "payload_cache.main:app", "--host", "0.0.0.0", "--port", "8000"]
