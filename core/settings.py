"""Config loading. Layered: base.yaml <- {mode}.yaml <- env overrides.

Safety invariants enforced here, not in the GUI:
  * mode defaults to PAPER; live requires --config config/live.yaml explicitly
  * ROBOTRADER_FORCE_PAPER env var can downgrade live->paper, never the reverse
  * entering live mode requires typing live_confirmation_phrase on the engine's
    controlling terminal (not the GUI) at startup
  * secrets come from keyring or env; they are excluded from repr/logging
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, SecretStr

from core.models import Mode

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class BrokerCreds(BaseModel):
    key_id: SecretStr
    secret_key: SecretStr


class Settings(BaseModel):
    mode: Mode = Mode.PAPER
    raw: dict  # full merged yaml; typed sub-models can be grown as needed

    model_config = {"frozen": True}


def load_settings(config_path: str | None = None) -> Settings:
    base = yaml.safe_load((CONFIG_DIR / "base.yaml").read_text())
    merged = dict(base)
    mode = Mode.PAPER
    if config_path:
        override = yaml.safe_load(Path(config_path).read_text()) or {}
        merged.update(override)
        mode = Mode(merged.get("mode", "paper"))
    if os.environ.get("ROBOTRADER_FORCE_PAPER"):
        mode = Mode.PAPER
    if mode is Mode.LIVE:
        _confirm_live(merged)
    return Settings(mode=mode, raw=merged)


def _confirm_live(cfg: dict) -> None:
    phrase = cfg["live_confirmation_phrase"]
    typed = input(f'LIVE MODE. Type the phrase to continue:\n  "{phrase}"\n> ')
    if typed.strip() != phrase:
        raise SystemExit("Live confirmation failed; refusing to start.")


def broker_creds(mode: Mode) -> BrokerCreds:
    """Load Alpaca credentials.

    Order of precedence:
      1. OS keyring (macOS Keychain, Windows Credential Manager, Secret Service)
      2. Environment variables (recommended for headless Linux/VPS)

    Secrets are never logged.
    """
    import os

    import keyring
    from keyring.errors import NoKeyringError

    prefix = "LIVE" if mode is Mode.LIVE else "PAPER"

    def get_secret(name: str) -> str:
        try:
            value = keyring.get_password("robotrader", name)
            if value:
                return value
        except NoKeyringError:
            # No usable keyring backend (common on headless Linux)
            pass

        return os.environ.get(f"ALPACA_{name}", "")

    key_id = get_secret(f"{prefix}_KEY_ID")
    secret = get_secret(f"{prefix}_SECRET_KEY")

    if not key_id or not secret:
        raise RuntimeError(
            f"Missing Alpaca {prefix.lower()} credentials.\n"
            f"Expected environment variables:\n"
            f"  ALPACA_{prefix}_KEY_ID\n"
            f"  ALPACA_{prefix}_SECRET_KEY"
        )

    return BrokerCreds(
        key_id=SecretStr(key_id),
        secret_key=SecretStr(secret),
    )
