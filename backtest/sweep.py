"""Programmatic entry point for the joint parameter sweep — backtest/runner.py's
counterpart for sweeps. Shared by scripts/sweep_full.py (CLI) and
service/sweep_jobs.py (GUI "Run Sweep" button). See scripts/sweep_full.py's
module docstring for the overfitting-discipline rationale this module
implements; this is just where the mechanics live so both callers share one
source of truth.
"""
from __future__ import annotations

import itertools
import json
import multiprocessing as mp
import os
import random
import tempfile
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pandas as pd

from backtest.engine import BacktestEngine
from backtest.metrics import stress_costs
from core.models import Mode
from core.settings import Settings, broker_creds, load_settings
from data.alpaca_data import AlpacaData
from journal.audit import AuditLog
from risk.manager import RiskManager
from strategies import build_strategy

# Every axis here already appears in sweep_params.py (signals) or
# sweep_allocation.py (allocation) — this sweep's contribution is searching
# them JOINTLY, not inventing new knobs.
GRID = {
    "tp_entry_limit_atr": [0, 0.25, 0.5],
    "tp_market_filter": [False, True],
    "tp_min_atr_pct": [0, 1.0, 1.5, 2.0],
    "tp_stop_atr_mult": [1.5, 2.0, 2.5],
    "momo_top_n": [1, 2],
    "momo_exit_rank": [2, 3, 4],
    "momo_stop_atr_mult": [2.5, 3.0, 3.5],
    "max_concurrent_positions": [3, 4, 5],
    "max_per_bucket": [1, 2],
    "max_position_notional_pct": [30, 40],
}

FOLDS = [("2016-01-04", "2018-12-31"), ("2019-01-01", "2021-12-31"),
         ("2022-01-01", "2023-12-31"), ("2024-01-01", None)]

_BARS: pd.DataFrame | None = None


def _init(bars_path: str) -> None:
    global _BARS
    _BARS = pd.read_parquet(bars_path)


def _apply(cfg: dict, knobs: dict) -> None:
    for entry in cfg["strategies"]:
        if entry["name"] == "trend_pullback":
            entry["params"]["entry_limit_atr"] = knobs["tp_entry_limit_atr"]
            entry["params"]["market_filter"] = knobs["tp_market_filter"]
            entry["params"]["min_atr_pct"] = knobs["tp_min_atr_pct"]
            entry["params"]["stop_atr_mult"] = knobs["tp_stop_atr_mult"]
        elif entry["name"] == "momentum_rotation":
            entry["params"]["top_n"] = knobs["momo_top_n"]
            entry["params"]["exit_rank"] = knobs["momo_exit_rank"]
            entry["params"]["stop_atr_mult"] = knobs["momo_stop_atr_mult"]
    for key in ("max_concurrent_positions", "max_per_bucket", "max_position_notional_pct"):
        cfg["risk"][key] = knobs[key]


def run_combo(job: tuple[dict, str | None, str | None]) -> dict:
    knobs, start, end = job
    cfg = deepcopy(load_settings().raw)
    _apply(cfg, knobs)
    settings = Settings(mode=Mode.BACKTEST, raw=cfg)
    risk = RiskManager(cfg, AuditLog(":memory:"))
    result = BacktestEngine(settings, build_strategy(cfg), risk).run(
        _BARS, start=start, end=end)
    m = result.metrics
    stress = stress_costs(result.trades, 2.0)
    return {**knobs, "trades": m["trades"], "sharpe": m.get("sharpe", 0),
            "pf": m.get("profit_factor", 0), "maxdd": m.get("max_drawdown_pct"),
            "ret": m.get("total_return_pct"), "dd_halts": m.get("drawdown_halts"),
            "expo": m.get("exposure_pct"), "pf2x": stress["profit_factor"],
            "ok2x": stress["profitable"]}


class SweepCancelled(Exception):
    pass


def _run_pool(ctx, workers: int, bars_path: str, jobs: list, cancel_flag: Callable[[], bool] | None) -> list:
    """pool.map() as a pollable, cancellable operation: map_async() doesn't
    block, so a "Stop" button can terminate the pool between polls instead
    of only being able to interrupt a blocking pool.map() call from another
    thread (undefined/version-dependent behavior in CPython's multiprocessing)."""
    pool = ctx.Pool(workers, initializer=_init, initargs=(bars_path,))
    async_result = pool.map_async(run_combo, jobs)
    while not async_result.ready():
        if cancel_flag is not None and cancel_flag():
            pool.terminate()
            pool.join()
            raise SweepCancelled()
        async_result.wait(0.5)
    pool.close()
    pool.join()
    return async_result.get()


def fmt(r: dict) -> str:
    knobs = " ".join(f"{k}={r[k]}" for k in GRID)
    return (f"{knobs}\n      n={r['trades']:>4} sharpe={r['sharpe']:>5.2f} "
            f"pf={r['pf']:>4.2f} dd={r['maxdd']:>5.2f}% ret={r['ret']:>6.1f}% "
            f"expo={r['expo']:>4.1f}% halts={r['dd_halts']} pf@2x={r['pf2x']:>4.2f} "
            f"{'OK' if r['ok2x'] else 'FAIL'}")


def run_sweep(
    n_samples: int = 250,
    workers: int = 5,
    is_end: str = "2021-12-31",
    oos_start: str = "2022-01-01",
    seed: int = 0,
    on_progress: Callable[[str], None] | None = None,
    cancel_flag: Callable[[], bool] | None = None,
) -> dict:
    """Runs the joint sweep, writes journal/sweeps/<run_id>/results.json, and
    returns that same structured dict (plus "run_id").

    Raises RuntimeError if paper API credentials aren't available, or
    SweepCancelled if cancel_flag() returns True while a pool is running
    (service/sweep_jobs.py's "Stop" button). Uses an explicit spawn context
    for every Pool — this is routinely called from a background thread
    inside the live FastAPI/uvicorn process, and forking worker processes
    from a thread that shares the server's event loop and open sockets is
    the kind of thing that hangs in ways that are hard to debug; spawn
    always starts clean.
    """
    def progress(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    # 1 vCPU droplets: more workers than cores just adds overhead.
    workers = max(1, min(workers, os.cpu_count() or 1))
    ctx = mp.get_context("spawn")

    full_size = 1
    for values in GRID.values():
        full_size *= len(values)
    n = min(n_samples, full_size)

    cfg = load_settings().raw
    creds = broker_creds(Mode.PAPER)            # raises RuntimeError if missing
    symbols = [s for ss in cfg["universe"]["buckets"].values() for s in ss]
    progress(f"Fetching {len(symbols)} symbols (cache-first)")
    data = AlpacaData(creds.key_id.get_secret_value(), creds.secret_key.get_secret_value())
    bars = data.daily_bars(symbols, "2016-01-04", pd.Timestamp.today().date().isoformat())
    bars_path = str(Path(tempfile.mkdtemp()) / "bars.parquet")
    bars.to_parquet(bars_path)

    rng = random.Random(seed)
    keys = list(GRID)
    all_combos = list(itertools.product(*GRID.values()))
    combos = [dict(zip(keys, values)) for values in rng.sample(all_combos, n)]

    progress(f"Running {len(combos)} in-sample combos ({workers} worker(s))")
    is_rows = _run_pool(ctx, workers, bars_path, [(c, None, is_end) for c in combos], cancel_flag)
    is_rows.sort(key=lambda r: (r["ok2x"], r["trades"] >= 150, r["sharpe"]), reverse=True)

    result: dict = {
        "full_grid_size": full_size, "n_samples": n, "is_end": is_end,
        "oos_start": oos_start, "seed": seed, "workers": workers,
        "is_top": is_rows[:10],
    }

    eligible = [r for r in is_rows if r["ok2x"] and r["trades"] >= 150]
    if not eligible:
        result["eligible"] = False
        return _save(result)
    top = eligible[:3]

    progress(f"Judging top {len(top)} out-of-sample")
    jobs = [({k: r[k] for k in GRID}, oos_start, None) for r in top]
    oos_rows = _run_pool(ctx, min(workers, len(jobs)), bars_path, jobs, cancel_flag)

    winner = {k: top[0][k] for k in GRID}
    progress("Running walk-forward folds for the winner")
    fold_rows = _run_pool(ctx, min(workers, len(FOLDS)), bars_path,
                           [(winner, a, b) for a, b in FOLDS], cancel_flag)
    profitable_folds = sum(1 for r in fold_rows if r["ret"] > 0)

    is_sharpe = top[0]["sharpe"]
    oos_sharpe = oos_rows[0]["sharpe"]
    overfit_warning = None
    if is_sharpe > 0 and oos_sharpe < is_sharpe * 0.6:
        overfit_warning = (f"IS Sharpe {is_sharpe:.2f} vs OOS Sharpe {oos_sharpe:.2f} — "
                            f"large IS/OOS gap is the classic overfitting signature.")

    result.update({
        "eligible": True,
        "oos_top": oos_rows,
        "winner": winner,
        "folds": [{"start": a, "end": b, **r} for (a, b), r in zip(FOLDS, fold_rows)],
        "profitable_folds": profitable_folds,
        "overfit_warning": overfit_warning,
    })
    return _save(result)


def _save(result: dict) -> dict:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    result["run_id"] = run_id
    out_dir = Path("journal/sweeps") / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(result, indent=2, default=str))
    return result
