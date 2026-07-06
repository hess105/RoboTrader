"""Round 3: portfolio-construction sweep (allocation, not signals).

The two-sleeve portfolio passes every Gate 1 criterion except IS Sharpe
(0.98 vs 1.0). Signal parameters are frozen — further signal tuning at this
point would be curve-fitting. Allocation knobs (how many slots, how big,
how concentrated) change how the same predictions are deployed, which is
the standard, lower-overfit-risk way to move Sharpe.

Selection rule (declared before running): highest IN-SAMPLE Sharpe subject
to IS max drawdown <= 12% and profitable at 2x costs. Winner judged once
out-of-sample and across the 4 walk-forward folds.

  python -m scripts.sweep_allocation
"""
from __future__ import annotations

import argparse
import tempfile
from copy import deepcopy
from itertools import product
from multiprocessing import Pool, set_start_method
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

GRID = {
    "momo_top_n": [1, 2],                    # momentum book: 1 concentrated vs 2 diversified
    "max_concurrent_positions": [3, 4, 5],   # total slots across both sleeves
    "max_per_bucket": [1, 2],                # correlation-bucket concurrency
    "max_position_notional_pct": [30, 40],   # single-position concentration cap
}

_BARS: pd.DataFrame | None = None


def _init(bars_path: str) -> None:
    global _BARS
    _BARS = pd.read_parquet(bars_path)


def run_combo(job: tuple[dict, str | None, str | None]) -> dict:
    knobs, start, end = job
    cfg = deepcopy(load_settings().raw)
    for entry in cfg["strategies"]:
        if entry["name"] == "momentum_rotation":
            entry["params"]["top_n"] = knobs["momo_top_n"]
    for key in ("max_concurrent_positions", "max_per_bucket", "max_position_notional_pct"):
        cfg["risk"][key] = knobs[key]
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


def fmt(r: dict) -> str:
    return (f"top{r['momo_top_n']} conc{r['max_concurrent_positions']} "
            f"bucket{r['max_per_bucket']} cap{r['max_position_notional_pct']} | "
            f"n={r['trades']:>4} sharpe={r['sharpe']:>5.2f} pf={r['pf']:>4.2f} "
            f"dd={r['maxdd']:>5.2f}% ret={r['ret']:>6.1f}% expo={r['expo']:>4.1f}% "
            f"halts={r['dd_halts']} pf@2x={r['pf2x']:>4.2f} "
            f"{'OK' if r['ok2x'] else 'FAIL'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--is-end", default="2021-12-31")
    ap.add_argument("--workers", type=int, default=5)
    args = ap.parse_args()

    cfg = load_settings().raw
    creds = broker_creds(Mode.PAPER)
    symbols = [s for ss in cfg["universe"]["buckets"].values() for s in ss]
    data = AlpacaData(creds.key_id.get_secret_value(), creds.secret_key.get_secret_value())
    bars = data.daily_bars(symbols, "2016-01-04", pd.Timestamp.today().date().isoformat())
    bars_path = str(Path(tempfile.mkdtemp()) / "bars.parquet")
    bars.to_parquet(bars_path)

    combos = [dict(zip(GRID, values)) for values in product(*GRID.values())]
    print(f"ALLOCATION SWEEP in-sample ..{args.is_end}: {len(combos)} combos")
    print("selection rule: max IS sharpe s.t. maxdd<=12 and ok2x\n")
    with Pool(args.workers, initializer=_init, initargs=(bars_path,)) as pool:
        rows = pool.map(run_combo, [(c, None, args.is_end) for c in combos])
    rows.sort(key=lambda r: r["sharpe"], reverse=True)
    for r in rows:
        print("  " + fmt(r))

    eligible = [r for r in rows if r["ok2x"] and (r["maxdd"] or 100) <= 12]
    if not eligible:
        print("\nNo eligible combo. Volatility targeting is the next lever.")
        return
    winner = {k: eligible[0][k] for k in GRID}
    print(f"\nWINNER {winner} — judged once OOS + folds + full:")
    jobs = [(winner, "2022-01-01", None),
            (winner, "2016-01-04", "2018-12-31"), (winner, "2019-01-01", "2021-12-31"),
            (winner, "2022-01-01", "2023-12-31"), (winner, "2024-01-01", None),
            (winner, None, None)]
    labels = ["OOS 2022..", "fold 16-18", "fold 19-21", "fold 22-23", "fold 24-now",
              "FULL 16-26"]
    with Pool(min(args.workers, len(jobs)), initializer=_init,
              initargs=(bars_path,)) as pool:
        out = pool.map(run_combo, jobs)
    for label, r in zip(labels, out):
        print(f"  {label:11} " + fmt(r))


if __name__ == "__main__":
    try:
        set_start_method("spawn")
    except RuntimeError:
        pass
    main()
