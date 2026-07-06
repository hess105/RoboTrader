"""Secret resolution for headless deployment (Docker; no OS keyring there).

Order per value: env var, then Docker Compose file-based secret mounted at
/run/secrets/<lowercased env var name>. `core/settings.py` still tries the
OS keyring first (macOS locally); this module is the fallback layer both it
and `monitoring/alerts.py` share so container secrets work the same way
everywhere a credential is read.
"""
from __future__ import annotations

import os
from pathlib import Path

SECRETS_DIR = Path("/run/secrets")


def read_secret(env_name: str) -> str:
    value = os.environ.get(env_name, "")
    if value:
        return value
    secret_file = SECRETS_DIR / env_name.lower()
    if secret_file.is_file():
        return secret_file.read_text().strip()
    return ""
