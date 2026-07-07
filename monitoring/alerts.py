"""Alert fan-out: Telegram (primary — free push to phone) + email fallback,
configured entirely via environment variables (see .env.example). Alert
delivery failures are printed and swallowed — alerting must never crash the
engine. Every alert also goes to stdout so the engine log is self-contained.

Severities: INFO (daily summary, reconnects), WARN (80% of a risk limit,
order retry), CRIT (disconnect, rejected order, limit breach, reconcile
mismatch, kill switch). Email is CRIT-only to avoid inbox noise.

Per-category push toggles (GUI Settings tab, `journal/alert_prefs.json`) let
Jeff mute a `kind` from push delivery without touching severity routing or
the audit trail — callers still journal independently of whether the push
goes out. `test` is exempt so the Settings-tab test button never silently
no-ops.
"""
from __future__ import annotations

import json
import smtplib
from email.message import EmailMessage
from pathlib import Path

import requests
import yaml

from core.secrets import read_secret

PREFS_PATH = Path("journal/alert_prefs.json")
CONFIG_PATH = Path("config/base.yaml")
DEFAULT_KINDS = ("engine", "risk", "order", "summary", "strategy",
                 "reconcile", "fill", "kill_switch", "health")


class Alerter:
    def __init__(self, cfg: dict | None = None):
        self.tg_token = read_secret("TELEGRAM_BOT_TOKEN")
        self.tg_chat = read_secret("TELEGRAM_CHAT_ID")
        self.email_to = read_secret("ALERT_EMAIL_TO")
        self.smtp_host = read_secret("SMTP_HOST")
        self.smtp_port = int(read_secret("SMTP_PORT") or "587")
        self.smtp_from = read_secret("SMTP_FROM")
        self.smtp_user = read_secret("SMTP_USER")
        self.smtp_password = read_secret("SMTP_PASSWORD")
        self.prefs = self._load_prefs()

    def _load_prefs(self) -> dict[str, bool]:
        if PREFS_PATH.is_file():
            try:
                return json.loads(PREFS_PATH.read_text())
            except Exception:                        # noqa: BLE001 — fall through to defaults
                pass
        enabled = set(DEFAULT_KINDS)
        try:
            base = yaml.safe_load(CONFIG_PATH.read_text()) or {}
            alert_on = base.get("alerts", {}).get("alert_on")
            if alert_on:
                seed = {
                    "disconnect": "health", "order_rejected": "order",
                    "risk_limit_breach": "risk", "kill_switch": "kill_switch",
                    "reconcile_mismatch": "reconcile", "daily_summary": "summary",
                }
                enabled = {seed.get(a, a) for a in alert_on}
        except Exception:                            # noqa: BLE001 — defaults still apply
            pass
        return {k: (k in enabled) for k in DEFAULT_KINDS}

    def set_pref(self, kind: str, enabled: bool) -> dict[str, bool]:
        self.prefs[kind] = enabled
        PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PREFS_PATH.write_text(json.dumps(self.prefs, indent=2))
        return dict(self.prefs)

    def send(self, severity: str, kind: str, message: str) -> None:
        line = f"[{severity}] robotrader/{kind}: {message}"
        print(f"ALERT {line}", flush=True)
        if kind != "test" and not self.prefs.get(kind, True):
            return
        for channel in (self._telegram, self._email):
            try:
                channel(severity, line)
            except Exception as exc:                 # noqa: BLE001 — never crash on alerting
                print(f"ALERT-DELIVERY-FAILED {channel.__name__}: {exc}", flush=True)

    def heartbeat(self) -> None:
        """Dead-man's ping (healthchecks.io or similar, HEALTHCHECKS_URL env).
        The engine calls this on every healthy tick; the monitoring service
        alerts when pings STOP — covering the one failure the process cannot
        report on itself. Best-effort, never raises."""
        url = read_secret("HEALTHCHECKS_URL")
        if not url:
            return
        try:
            requests.get(url, timeout=5)
        except Exception:                            # noqa: BLE001
            pass

    def _telegram(self, severity: str, line: str) -> None:
        if not (self.tg_token and self.tg_chat):
            return
        requests.post(
            f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
            json={"chat_id": self.tg_chat, "text": line}, timeout=5,
        )

    def _email(self, severity: str, line: str) -> None:
        if severity != "CRIT" or not (self.smtp_host and self.email_to):
            return
        msg = EmailMessage()
        msg["Subject"] = line[:120]
        msg["From"] = self.smtp_from or self.smtp_user or "robotrader@localhost"
        msg["To"] = self.email_to
        msg.set_content(line)
        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as s:
            s.starttls()
            if self.smtp_user:
                s.login(self.smtp_user, self.smtp_password)
            s.send_message(msg)
