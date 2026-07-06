# Running RoboTrader 24/7 on a KVM VPS

Any KVM (or fully-virtualized) VPS works: 1 vCPU, 1 GB RAM, 10 GB disk is
plenty — the engine computes signals once a day and ticks once a minute.
Hetzner CX22 (~€4/mo), Vultr/DigitalOcean/Linode ($6/mo) are all fine.
Region barely matters for daily bars; US East is a reasonable default.
OS assumed below: Ubuntu 24.04.

## 1. Base box

```bash
adduser robotrader
apt update && apt install -y python3.12-venv git make ufw tmux
ufw default deny incoming && ufw allow OpenSSH && ufw enable
# SSH keys only:
sed -i 's/#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl reload ssh
```

Server timezone doesn't matter — the scheduler pins America/New_York
internally (service/engine.py).

## 2. Install

```bash
sudo -iu robotrader
git clone <your-repo-url> /opt/robotrader && cd /opt/robotrader
make install
```

## 3. Credentials — env file, not keychain

macOS Keychain doesn't exist here, and headless Linux has no Secret Service
for `keyring`, so use the env-var fallback that `core/settings.py` already
supports. As root:

```bash
cat > /etc/robotrader.env << 'EOF'
ALPACA_PAPER_KEY_ID=...
ALPACA_PAPER_SECRET_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
ALERT_EMAIL_TO=you@example.com
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_FROM=robotrader@example.com
SMTP_USER=robotrader@example.com
SMTP_PASSWORD=...
HEALTHCHECKS_URL=https://hc-ping.com/<your-uuid>
EOF
chmod 600 /etc/robotrader.env && chown robotrader /etc/robotrader.env
```

Do NOT add live keys until Gate 2 passes.

## 4. GUI

The engine serves the built dashboard at :8765. Either install node on the
server (`apt install -y nodejs npm`, then `make gui-build`), or build on
your laptop and copy it:

```bash
make gui-build && rsync -a gui/web/dist/ robotrader@server:/opt/robotrader/gui/web/dist/
```

## 5. Remote access — Tailscale, never an open port

The API/GUI binds 127.0.0.1 and has no authentication; exposing :8765
publicly would hand your kill switch to the internet. Install Tailscale on
the server and your phone/laptop:

```bash
curl -fsSL https://tailscale.com/install.sh | sh && tailscale up
```

Then either browse to `http://<tailscale-ip>:8765` after setting
`service.host: 0.0.0.0` in config (safe ONLY because ufw blocks everything
and Tailscale traffic arrives on its own interface — keep the firewall on),
or leave host as 127.0.0.1 and use an SSH tunnel:
`ssh -L 8765:127.0.0.1:8765 robotrader@server`.

## 6. Run as a service (paper)

```bash
sudo cp deploy/robotrader.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now robotrader
systemctl status robotrader
```

`Restart=on-failure` + the healthchecks.io dead-man ping (the engine pings
`HEALTHCHECKS_URL` every healthy tick; healthchecks alerts you if pings
stop) covers the two ways a remote box fails silently.

## 7. Live mode (later, after Gate 2)

Live requires typing the confirmation phrase interactively, so it does NOT
run under systemd — that's intentional; a service manager cannot consent to
real money. Run it in tmux:

```bash
tmux new -s robotrader
cd /opt/robotrader && make live      # type the phrase; detach with Ctrl-B D
```

## 8. Backups + drills

The journal is the system of record (orders, fills, tax lots, halts):

```bash
# as robotrader: crontab -e
15 22 * * 1-5 sqlite3 /opt/robotrader/journal/audit.sqlite ".backup /opt/robotrader/journal/audit.$(date +\%u).bak"
```

Monthly, from anywhere with SSH access, drill the GUI-independent kill path:
`ssh robotrader@server 'cd /opt/robotrader && make kill'`.
