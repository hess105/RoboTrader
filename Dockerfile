# syntax=docker/dockerfile:1

# No Node build stage on purpose: building the Vite/TypeScript dashboard
# is CPU/memory-heavy and pointless to run on a $6/mo droplet every
# deploy. Build it once on your laptop (`make gui-build`) and it's served
# via the bind mount in docker-compose.yml (./gui/web/dist), same as
# config/. This image only ever needs Python.
FROM python:3.12-slim AS runtime

# Explicit, not the default debconf auto-fallback: there's no TTY in a
# Docker build, so apt would otherwise print "unable to initialize
# frontend: Teletype" warnings (harmless, but noisy) before falling back
# to this anyway.
ENV DEBIAN_FRONTEND=noninteractive

# gcc: some deps (e.g. numba/llvmlite via vectorbt) need to build from
# source on platforms without a prebuilt wheel; sqlite3: CLI for
# journal/audit.sqlite inspection and manual .backup during operations.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        sqlite3 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 robotrader
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p gui/web/dist journal/backtests logs data/cache exports \
    && chown -R robotrader:robotrader /app

USER robotrader

EXPOSE 8765

CMD ["python", "-m", "service.engine", "--config", "config/paper.yaml"]
