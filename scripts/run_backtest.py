"""Run a gate-qualifying backtest and print gate metrics.

  python -m scripts.run_backtest
  python -m scripts.run_backtest --start 2016-01-01 --end 2026-06-30

Fetches ~1.5y of extra history before --start for indicator warmup; trading
begins at --start. Writes journal/backtests/<run_id>/ (equity curve, trades,
metrics, config snapshot) for the GUI results viewer and the gate checklist.
Requires paper API keys (make keys-paper) — Alpaca's free IEX data works
with paper credentials.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime

import pandas as pd

from backtest.engine import BacktestEngine
from backtest.metrics import stress_costs
from core.models import Mode
from core.settings import broker_creds, load_settings
from data.alpaca_data import AlpacaData
from journal.audit import AuditLog
from risk.manager import RiskManager
from strategies import build_strategy


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    settings = load_settings()          # base config; backtests never touch live
    cfg = settings.raw
    try:
        creds = broker_creds(Mode.PAPER)
    except RuntimeError:
        raise SystemExit(
            "No paper API credentials found.\n"
            "Run `make keys-paper` (or set ALPACA_PAPER_KEY_ID / "
            "ALPACA_PAPER_SECRET_KEY) first — free Alpaca paper keys are "
            "enough for historical data."
        )

    start = args.start or cfg["backtest"]["start"]
    end = args.end or date.today().isoformat()
    fetch_start = (pd.Timestamp(start) - pd.Timedelta(days=550)).date().isoformat()
    symbols = [s for syms in cfg["universe"]["buckets"].values() for s in syms]

    print(f"Fetching {len(symbols)} symbols {fetch_start} .. {end} (cache-first)")
    data = AlpacaData(creds.key_id.get_secret_value(),
                      creds.secret_key.get_secret_value())
    bars = data.daily_bars(symbols, fetch_start, end)

    strategy = build_strategy(cfg)
    risk = RiskManager(cfg, AuditLog(":memory:"))
    print(f"Running {strategy.name} {start} .. {end}")
    result = BacktestEngine(settings, strategy, risk).run(bars, start=start, end=end)

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = f"journal/backtests/{run_id}"
    result.save(out_dir)

    m = result.metrics
    print(f"\n=== {run_id} ===")
    for k, v in m.items():
        print(f"  {k:20} {v}")
    stress = stress_costs(result.trades, 2.0)
    print(f"\n  2x-cost stress: pnl={stress['total_pnl']} pf={stress['profit_factor']} "
          f"profitable={stress['profitable']}")

    print("\nGate 1 quick check (full criteria in docs/GATES.md):")
    checks = [
        ("trades >= 200", m.get("trades", 0) >= 200),
        ("sharpe >= 1.0", (m.get("sharpe") or 0) >= 1.0),
        ("max drawdown <= 15%", (m.get("max_drawdown_pct") or 100) <= 15),
        ("profit factor >= 1.3", (m.get("profit_factor") or 0) >= 1.3),
        ("survives 2x costs", stress["profitable"]),
    ]
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print(f"\nSaved to {out_dir}/ — walk-forward folds and Sharpe>2.5 sanity "
          f"review are still manual for now.")


if __name__ == "__main__":
    main()
