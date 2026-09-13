# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.12.3 AS uv

FROM python:3.12-slim-bookworm AS build
COPY --from=uv /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

FROM python:3.12-slim-bookworm
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NOTEIQ_HOST=0.0.0.0 \
    PORT=8000 \
    NOTEIQ_DATABASE=/tmp/noteiq.sqlite3
WORKDIR /app

RUN groupadd --gid 10001 noteiq \
    && useradd --uid 10001 --gid noteiq --no-create-home --shell /usr/sbin/nologin noteiq \
    && mkdir /data \
    && chown noteiq:noteiq /data

COPY --from=build --chown=noteiq:noteiq /app/.venv /app/.venv
COPY --chown=noteiq:noteiq app /app/app
COPY --chown=noteiq:noteiq scripts /app/scripts
COPY --chown=noteiq:noteiq web /app/web

USER noteiq
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
CMD ["python", "-m", "scripts.serve"]
