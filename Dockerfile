# syntax=docker/dockerfile:1

########################################
# Stage 1: build the React/Vite dashboard
########################################
FROM node:20-slim AS gui-builder
WORKDIR /gui
COPY gui/web/package.json gui/web/package-lock.json ./
RUN npm ci
COPY gui/web ./
RUN npm run build

########################################
# Stage 2: runtime image
########################################
FROM python:3.12-slim AS runtime

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
COPY --from=gui-builder /gui/dist ./gui/web/dist

RUN mkdir -p journal/backtests logs data/cache exports \
    && chown -R robotrader:robotrader /app

USER robotrader

EXPOSE 8765

CMD ["python", "-m", "service.engine", "--config", "config/paper.yaml"]
