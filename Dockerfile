# syntax=docker/dockerfile:1
FROM node:24.19.0-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY v2/frontend/package.json v2/frontend/package-lock.json v2/frontend/.npmrc ./
RUN npm ci --no-audit --no-fund
COPY v2/frontend/ ./
RUN npm run build

FROM python:3.13.13-slim-bookworm AS dependencies
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY v2/backend/requirements.lock.txt /tmp/requirements.lock.txt
RUN /opt/venv/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock.txt

FROM python:3.13.13-slim-bookworm AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    XDG_CACHE_HOME=/tmp/.cache \
    XDG_CONFIG_HOME=/tmp/.config \
    KB_DATA_DIR=/app/data \
    DATABASE_URL=sqlite:////app/data/app.db \
    WIKI_UPSTREAM_DB_PATH=/app/data/knowledge.db
COPY --from=dependencies /opt/venv /opt/venv
RUN python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 aipartner \
    && useradd --uid 10001 --gid aipartner --create-home aipartner \
    && mkdir -p /app/data \
    && chown aipartner:aipartner /app/data
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
ENV TZ=Asia/Shanghai
WORKDIR /app/v2/backend
COPY --chown=aipartner:aipartner v2/backend/ ./
COPY --chown=aipartner:aipartner v2/scripts/ /app/v2/scripts/
COPY --from=frontend /build/frontend/dist /app/v2/frontend/dist
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3).close()"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
