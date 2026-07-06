#!/usr/bin/env bash
set -Eeuo pipefail

############################################
# RoboTrader Docker Installer
# Ubuntu 24.04+ (DigitalOcean droplet or any KVM VPS)
############################################

APP_USER="robotrader"
APP_DIR="/opt/robotrader"
REPO_URL="https://github.com/hess105/RoboTrader.git"

SECRET_NAMES=(
    alpaca_paper_key_id
    alpaca_paper_secret_key
    alpaca_live_key_id
    alpaca_live_secret_key
    telegram_bot_token
    telegram_chat_id
    alert_email_to
    smtp_host
    smtp_port
    smtp_from
    smtp_user
    smtp_password
    healthchecks_url
)

############################################

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo."
    exit 1
fi

echo "====================================="
echo "Installing RoboTrader (Docker)"
echo "====================================="

apt update
apt install -y ca-certificates curl gnupg ufw fail2ban

############################################
# Docker Engine + Compose plugin
############################################

if ! command -v docker >/dev/null; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc

    . /etc/os-release
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
      > /etc/apt/sources.list.d/docker.list

    apt update
    apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

systemctl enable --now docker

############################################
# User
############################################

if ! id "$APP_USER" >/dev/null 2>&1; then
    adduser --disabled-password --gecos "" "$APP_USER"
fi
usermod -aG docker "$APP_USER"

############################################
# Firewall
############################################

ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

systemctl enable fail2ban
systemctl start fail2ban

############################################
# Clone / Update
############################################

if [[ ! -d "$APP_DIR/.git" ]]; then
    mkdir -p "$APP_DIR"
    chown "$APP_USER:$APP_USER" "$APP_DIR"
    sudo -u "$APP_USER" git clone "$REPO_URL" "$APP_DIR"
else
    sudo -u "$APP_USER" git -C "$APP_DIR" pull
fi

############################################
# Secrets (Docker Compose file-based secrets)
############################################

sudo -u "$APP_USER" mkdir -p "$APP_DIR/secrets" "$APP_DIR/journal/backtests" "$APP_DIR/logs" "$APP_DIR/data/cache" "$APP_DIR/exports"

for name in "${SECRET_NAMES[@]}"; do
    f="$APP_DIR/secrets/${name}.txt"
    if [[ ! -f "$f" ]]; then
        sudo -u "$APP_USER" touch "$f"
        chmod 600 "$f"
    fi
done

############################################
# Build
############################################

sudo -u "$APP_USER" bash -c "cd $APP_DIR && docker compose build"

############################################
# Optional Tailscale (remote access to the GUI without opening the port)
############################################

if ! command -v tailscale >/dev/null; then
    read -rp "Install Tailscale? (y/N): " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
        curl -fsSL https://tailscale.com/install.sh | sh
        echo
        echo "Run: sudo tailscale up"
    fi
fi

############################################
# Done
############################################

echo
echo "Edit your Alpaca paper keys (required before starting):"
echo
echo "  sudo -u $APP_USER nano $APP_DIR/secrets/alpaca_paper_key_id.txt"
echo "  sudo -u $APP_USER nano $APP_DIR/secrets/alpaca_paper_secret_key.txt"
echo
echo "Then start the engine (paper mode, restarts on failure/reboot):"
echo
echo "  su - $APP_USER -c 'cd $APP_DIR && docker compose up -d'"
echo
echo "View logs:"
echo
echo "  su - $APP_USER -c 'cd $APP_DIR && docker compose logs -f'"
echo
echo "GUI (after Tailscale or an SSH tunnel): http://<tailscale-ip>:8765"
