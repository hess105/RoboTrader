#!/usr/bin/env bash
set -Eeuo pipefail

############################################
# Run this on YOUR LAPTOP, never on the droplet.
#
# Pushes a full update: git push, build the dashboard, rsync it to the
# droplet, then git pull + rebuild the (Python-only) image + restart the
# engine there. Steps 3a/8 of docs/DEPLOY.md, chained into one command.
#
# Usage:
#   deploy/push-update.sh                     # uses defaults below
#   deploy/push-update.sh <user>@<droplet-ip>  # override target for this run
#   DROPLET_HOST=1.2.3.4 deploy/push-update.sh # override via env var
############################################

DROPLET_USER="${DROPLET_USER:-robotrader}"
DROPLET_HOST="${DROPLET_HOST:-159.203.188.196}"
REMOTE_DIR="${REMOTE_DIR:-/opt/robotrader}"

if [[ $# -ge 1 ]]; then
    TARGET="$1"
else
    TARGET="$DROPLET_USER@$DROPLET_HOST"
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -n "$(git status --porcelain)" ]]; then
    echo "WARNING: you have uncommitted local changes — they will NOT be pushed:"
    git status --short
    read -rp "Continue anyway? (y/N): " ans
    [[ "$ans" =~ ^[Yy]$ ]] || exit 1
fi

echo "==> Pushing local commits"
git push

echo "==> Building the dashboard (gui/web/dist)"
make gui-build

echo "==> Syncing the dashboard to $TARGET"
rsync -az --delete gui/web/dist/ "$TARGET:$REMOTE_DIR/gui/web/dist/"

echo "==> Updating the droplet: git pull, rebuild image, restart"
ssh "$TARGET" "cd $REMOTE_DIR && git pull && docker compose build && docker compose up -d"

echo "==> Done. Current status:"
ssh "$TARGET" "cd $REMOTE_DIR && docker compose ps"
