"""Run a gate-qualifying backtest and print gate metrics.

  python -m scripts.run_backtest
  python -m scripts.run_backtest --start 2016-01-01 --end 2026-06-30

Fetches ~1.5y of extra history before --start for indicator warmup; trading
begins at --start. Writes journal/backtests/<run_id>/ (equity curve, trades,
metrics, gate1 check, config snapshot) for the GUI results viewer and the
gate checklist. The GUI's Config tab "Run Backtest" button runs the same
underlying backtest/runner.py — this script is just its CLI face.
Requires paper API keys (make keys-paper) — Alpaca's free IEX data works
with paper credentials.
"""
from __future__ import annotations

import argparse

from backtest.runner import run_backtest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--label", default=None, help="display name shown in the GUI Results table")
    args = ap.parse_args()

    try:
        out = run_backtest(args.start, args.end, on_progress=print, label=args.label)
    except RuntimeError:
        raise SystemExit(
            "No paper API credentials found.\n"
            "Run `make keys-paper` (or set ALPACA_PAPER_KEY_ID / "
            "ALPACA_PAPER_SECRET_KEY) first — free Alpaca paper keys are "
            "enough for historical data."
        )

    run_id = out["run_id"]
    print(f"\n=== {run_id} ===")
    for k, v in out["metrics"].items():
        print(f"  {k:20} {v}")
    s = out["stress"]
    print(f"\n  2x-cost stress: pnl={s['total_pnl']} pf={s['profit_factor']} "
          f"profitable={s['profitable']}")

    print("\nGate 1 quick check (full criteria in docs/GATES.md):")
    for check in out["gate1"]:
        print(f"  [{'PASS' if check['ok'] else 'FAIL'}] {check['label']}")
    print(f"\nSaved to journal/backtests/{run_id}/ — walk-forward folds and "
          f"Sharpe>2.5 sanity review are still manual for now.")


if __name__ == "__main__":
    main()
