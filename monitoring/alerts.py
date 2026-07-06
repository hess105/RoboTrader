"""Alert fan-out: Telegram (primary — free push to phone) + email fallback,
configured entirely via environment variables (see .env.example). Alert
delivery failures are printed and swallowed — alerting must never crash the
engine. Every alert also goes to stdout so the engine log is self-contained.

Severities: INFO (daily summary, reconnects), WARN (80% of a risk limit,
order retry), CRIT (disconnect, rejected order, limit breach, reconcile
mismatch, kill switch). Email is CRIT-only to avoid inbox noise.
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

import requests


class Alerter:
    def __init__(self, cfg: dict | None = None):
        self.tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.email_to = os.environ.get("ALERT_EMAIL_TO", "")
        self.smtp_host = os.environ.get("SMTP_HOST", "")
        self.smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        self.smtp_from = os.environ.get("SMTP_FROM", "")
        self.smtp_user = os.environ.get("SMTP_USER", "")
        self.smtp_password = os.environ.get("SMTP_PASSWORD", "")

    def send(self, severity: str, kind: str, message: str) -> None:
        line = f"[{severity}] robotrader/{kind}: {message}"
        print(f"ALERT {line}", flush=True)
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
        url = os.environ.get("HEALTHCHECKS_URL", "")
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
