"""Store broker API keys in the OS keychain (macOS Keychain via `keyring`).

  python -m scripts.store_keys --mode paper

Prompts with getpass (no echo, no shell history), writes to keyring service
'robotrader'. After this, .env is unnecessary for credentials.
"""
from __future__ import annotations

import argparse
import getpass

import keyring

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["paper", "live"], required=True)
    mode = p.parse_args().mode.upper()
    keyring.set_password("robotrader", f"{mode}_KEY_ID", getpass.getpass(f"{mode} key id: "))
    keyring.set_password("robotrader", f"{mode}_SECRET_KEY", getpass.getpass(f"{mode} secret: "))
    print(f"{mode} credentials stored in keychain.")
