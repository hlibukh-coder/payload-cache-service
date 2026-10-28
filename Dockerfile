# syntax=docker/dockerfile:1
# Patch-level tag, so two builds of the same commit get the same interpreter.
FROM python:3.13.18-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Lives on a volume so the cache outlives the container (see docker-compose.yml).
    DATABASE_URL=sqlite:////data/payload_cache.db

WORKDIR /app

# Dependencies first, from the lock file, so editing source code does not invalidate this
# (slow) layer and every build installs exactly the versions the tests ran against. The lock
# includes the PostgreSQL driver so the same image works with either backend; DATABASE_URL
# picks one. README.md explains how to regenerate it.
COPY requirements.lock ./
RUN pip install -r requirements.lock

COPY pyproject.toml README.md ./
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

CMD ["uvicorn", "--factory", "payload_cache.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
