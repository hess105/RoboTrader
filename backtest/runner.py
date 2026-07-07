"""Programmatic entry point for a gate-qualifying backtest run — shared by
scripts/run_backtest.py (CLI) and service/backtest_jobs.py (GUI "Run
Backtest" button), so there is exactly one place that fetches data, builds
the strategy, and runs the engine.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from backtest.engine import BacktestEngine
from backtest.metrics import stress_costs
from core.models import Mode
from core.settings import broker_creds, load_settings
from data.alpaca_data import AlpacaData
from journal.audit import AuditLog
from risk.manager import RiskManager
from strategies import build_strategy


def run_backtest(
    start: str | None = None,
    end: str | None = None,
    on_progress: Callable[[str], None] | None = None,
    label: str | None = None,
) -> dict:
    """Fetches data, runs the backtest, writes journal/backtests/<run_id>/
    (equity, trades, metrics, a gate1.json quick-check summary, and — if
    given — a meta.json holding the operator's display label), and returns
    {"run_id", "label", "metrics", "gate1", "stress"}.

    run_id itself stays a sortable timestamp (it's the directory name and
    must be unique); label is purely a display name layered on top, never
    used for identity.

    Raises RuntimeError if paper API credentials aren't available — the
    same failure mode whether triggered from the CLI or the GUI.
    """
    def progress(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    settings = load_settings()          # base config; backtests never touch live
    cfg = settings.raw
    creds = broker_creds(Mode.PAPER)    # raises RuntimeError if missing

    start = start or cfg["backtest"]["start"]
    end = end or date.today().isoformat()
    fetch_start = (pd.Timestamp(start) - pd.Timedelta(days=550)).date().isoformat()
    symbols = [s for syms in cfg["universe"]["buckets"].values() for s in syms]

    progress(f"Fetching {len(symbols)} symbols {fetch_start} .. {end} (cache-first)")
    data = AlpacaData(creds.key_id.get_secret_value(),
                      creds.secret_key.get_secret_value())
    bars = data.daily_bars(symbols, fetch_start, end)

    strategy = build_strategy(cfg)
    risk = RiskManager(cfg, AuditLog(":memory:"))
    progress(f"Running {strategy.name} {start} .. {end}")
    result = BacktestEngine(settings, strategy, risk).run(bars, start=start, end=end)

    progress("Saving results")
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = f"journal/backtests/{run_id}"
    result.save(out_dir)

    m = result.metrics
    stress = stress_costs(result.trades, 2.0)
    gate1 = [
        {"label": "trades >= 200", "ok": m.get("trades", 0) >= 200},
        {"label": "sharpe >= 1.0", "ok": (m.get("sharpe") or 0) >= 1.0},
        {"label": "max drawdown <= 15%", "ok": (m.get("max_drawdown_pct") or 100) <= 15},
        {"label": "profit factor >= 1.3", "ok": (m.get("profit_factor") or 0) >= 1.3},
        {"label": "survives 2x costs", "ok": stress["profitable"]},
    ]
    Path(out_dir, "gate1.json").write_text(json.dumps({"gate1": gate1, "stress": stress}, indent=2, default=str))
    if label:
        Path(out_dir, "meta.json").write_text(json.dumps({"label": label}))

    return {"run_id": run_id, "label": label, "metrics": m, "gate1": gate1, "stress": stress}
