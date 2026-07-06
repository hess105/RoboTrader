"""Kill switch CLI — the GUI-independent path to flat.

  python -m scripts.kill --reason "manual"

POSTs /killswitch to the local engine. If the engine API is unreachable,
falls back to calling Alpaca directly (cancel all orders, close all
positions) with keychain credentials, then exits nonzero so you know the
engine itself needs attention. Test it in paper monthly.
"""
from __future__ import annotations

import argparse
import sys

import requests


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reason", default="manual CLI kill")
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    ap.add_argument("--mode", choices=["paper", "live"], default="paper",
                    help="broker to flatten in the direct-fallback path")
    args = ap.parse_args()

    try:
        r = requests.post(f"{args.url}/killswitch", timeout=90,
                          json={"confirm": "FLATTEN", "reason": args.reason})
        r.raise_for_status()
        body = r.json()
        print(f"engine kill switch: flat={body['flat']} halt={body['halt']}")
        return 0 if body["flat"] else 1
    except requests.ConnectionError:
        print("Engine API unreachable — falling back to DIRECT broker flatten.")

    from core.models import Mode
    from core.settings import broker_creds
    from execution.alpaca_client import AlpacaExecution

    mode = Mode.LIVE if args.mode == "live" else Mode.PAPER
    broker = AlpacaExecution(broker_creds(mode), mode)
    for order in broker.open_orders():
        broker.cancel(order.client_order_id)
    broker.close_all_positions()
    remaining = broker.positions()
    print(f"direct flatten done; positions remaining: {len(remaining)}")
    print("NOTE: engine was not running — investigate before restarting it.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
