# Running RoboTrader 24/7 in Docker on a DigitalOcean droplet

Deployment is Docker-only. Any small Ubuntu droplet works: 1 vCPU / 1 GB RAM
/ 25 GB disk (DigitalOcean's cheapest droplet) is plenty — the engine
computes signals once a day and ticks once a minute. OS assumed below:
Ubuntu 24.04 LTS. Region barely matters for daily bars; NYC is a reasonable
default. Everything here also works on Hetzner/Vultr/Linode — it's a
generic Ubuntu + Docker setup, DigitalOcean is just the example.

Server timezone doesn't matter — the scheduler pins America/New_York
internally (`service/engine.py`).

## 1. Create the droplet

Ubuntu 24.04 LTS, the cheapest plan, SSH-key auth (skip the password root
login DigitalOcean offers). Note the droplet's IP.

## 2. One-command install

SSH in as root, then:

```bash
curl -fsSL https://raw.githubusercontent.com/hess105/RoboTrader/main/deploy/docker-install.sh -o docker-install.sh
bash docker-install.sh
```

This script (`deploy/docker-install.sh`):

- installs Docker Engine + the Compose plugin from Docker's official apt repo
- creates a dedicated `robotrader` system user (in the `docker` group, not `sudo`)
- enables `ufw` (SSH only) and `fail2ban`
- clones the repo to `/opt/robotrader`
- adds a 2 GB swapfile if none exists (cheap droplets ship with none, and
  `pip install` alone can get close to 1 GB of RAM on the smallest plan)
- creates empty, `chmod 600` placeholder files under `/opt/robotrader/secrets/`
  for every credential (Docker Compose secrets need the file to exist even
  if you don't use that channel — e.g. you can leave the live-key and SMTP
  files empty if you only use Telegram alerts and paper trading)
- builds the Docker image — **Python only.** The image never runs Node/npm;
  see step 3a for the dashboard.

If you'd rather read it before running it, it's a plain bash script — see
[deploy/docker-install.sh](../deploy/docker-install.sh).

## 3a. The dashboard — built on your laptop, not the droplet

Building the React/Vite dashboard (`npm ci` + `tsc` + `vite build`) is
CPU/memory-heavy for what a $6/mo droplet has to offer, and there's no reason
to pay that cost on every deploy. So the Docker image deliberately contains
no Node at all — `docker-compose.yml` bind-mounts `gui/web/dist` into the
container the same way it mounts `config/`. If that directory is empty, the
engine just serves a "GUI not built" JSON fallback at `:8765` instead of
failing.

On your laptop (which has Node already, from local dev):

```bash
make gui-build      # writes gui/web/dist/
rsync -az gui/web/dist/ robotrader@<droplet-ip>:/opt/robotrader/gui/web/dist/
```

Do this once, and again any time you change the dashboard. Everything else
(`git pull` on the droplet, `docker compose build`) never touches it.

## 3b. Credentials — Docker secrets, not the OS keychain

Headless Linux has no Secret Service for `keyring`, so `core/settings.py`
falls back to reading credentials from environment variables, or — new for
Docker — from files Compose mounts read-only at `/run/secrets/<name>`
(see `core/secrets.py`). The installer already created the files; fill in
the ones you need:

```bash
sudo -u robotrader nano /opt/robotrader/secrets/alpaca_paper_key_id.txt
sudo -u robotrader nano /opt/robotrader/secrets/alpaca_paper_secret_key.txt

# Optional (leave the file empty to disable that alert channel):
sudo -u robotrader nano /opt/robotrader/secrets/telegram_bot_token.txt
sudo -u robotrader nano /opt/robotrader/secrets/telegram_chat_id.txt
sudo -u robotrader nano /opt/robotrader/secrets/healthchecks_url.txt
```

Each file holds exactly one value, no quotes, no trailing newline needed
(it's stripped). Do NOT put live keys in until Gate 2 passes.

## 4. Remote access — Tailscale, never an open port

`docker-compose.yml` only publishes the API/GUI on the **droplet's own**
`127.0.0.1:8765` (`ports: ["127.0.0.1:8765:8765"]`) — it is never reachable
from the public internet, ufw or not. Install Tailscale on the droplet and
your phone/laptop (the installer offers to do this for you):

```bash
curl -fsSL https://tailscale.com/install.sh | sh && tailscale up
```

Then either browse to `http://<tailscale-ip>:8765` (safe because Tailscale
traffic arrives on its own interface, and the port is bound to loopback for
everything else), or SSH-tunnel instead:
`ssh -L 8765:127.0.0.1:8765 robotrader@<droplet-ip>`.

## 5. Run as a service (paper)

```bash
su - robotrader
cd /opt/robotrader
docker compose up -d       # or: make docker-up
docker compose logs -f     # or: make docker-logs
```

`restart: unless-stopped` in `docker-compose.yml` covers both crash recovery
and droplet reboots (Docker itself is enabled via systemd by the installer),
the same job the old systemd unit used to do. The healthchecks.io dead-man
ping (the engine pings the URL in `secrets/healthchecks_url.txt` every
healthy tick; healthchecks alerts you if pings stop) covers the other way a
remote box fails silently.

## 6. Live mode (later, after Gate 2)

Live requires typing the confirmation phrase on an interactive terminal
(`core/settings.py: _confirm_live`), so it deliberately does NOT run as the
long-lived `engine` service — a container restart policy cannot consent to
real money. Run it attached, the Docker equivalent of the old tmux session:

```bash
cd /opt/robotrader
docker compose run --rm engine python -m service.engine --config config/live.yaml
# or: make docker-live
```

`run` reuses the same image, volumes and secrets as the `engine` service but
keeps your terminal attached, so the typed-phrase gate still works exactly
as before. Detach with Ctrl-C only after confirming you want it stopped —
there's no `-d` here on purpose.

## 7. Backups + drills

The journal is the system of record (orders, fills, tax lots, halts) and
lives on the host at `/opt/robotrader/journal` (bind-mounted, not a Docker
volume, specifically so host-side cron/backup tooling can see it directly):

```bash
# as robotrader: crontab -e
15 22 * * 1-5 sqlite3 /opt/robotrader/journal/audit.sqlite ".backup /opt/robotrader/journal/audit.$(date +\%u).bak"
```

Monthly, from anywhere with SSH access, drill the GUI-independent kill path:

```bash
ssh robotrader@<droplet-ip> 'cd /opt/robotrader && docker compose exec engine python -m scripts.kill --reason "monthly drill"'
# or: ssh robotrader@<droplet-ip> 'cd /opt/robotrader && make docker-kill'
```

## 8. Updating

```bash
su - robotrader
cd /opt/robotrader
git pull
docker compose build
docker compose up -d
```

Config changes in `config/*.yaml` take effect on the next restart — they're
bind-mounted read-only, not baked into the image, so `git pull` alone is
enough for a config-only change (`docker compose restart engine`). If the
dashboard itself changed, redo step 3a's `make gui-build` + `rsync` too —
`git pull` alone does not update it.

## Local development (macOS or otherwise, no Docker)

Docker is for deployment; day-to-day development still uses the venv/`make`
workflow (`make install`, `make test`, `make backtest`, `make paper`, `make
gui`) — see the [README](../README.md). The OS keychain path
(`make keys-paper`) only applies there; on the droplet, credentials always
come from the `secrets/` files above.
