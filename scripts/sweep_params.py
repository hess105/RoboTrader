"""Parameter sweep for Gate 1 iteration — research tool, not a gate artifact.

Discipline against overfitting:
  * small grid, every axis economically motivated (washout depth, bounce
    capture, stop width, patience) — no data mining over dozens of knobs
  * selection happens ONLY on the in-sample window (--is-end and earlier)
  * the winner is then evaluated once on untouched out-of-sample data and
    across 4 walk-forward folds (GATES: profitable in >= 3 of 4)
  * final numbers come from `make backtest` on the updated config, not here

  python -m scripts.sweep_params            # IS 2016..2021, OOS 2022..
"""
from __future__ import annotations

import argparse
import tempfile
from copy import deepcopy
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

# Round 2: structural axes. Round 1 (rsi/exit/stop/hold grid) showed no
# parameter combo passes Gate 1; rsi<10/exit>70/stop 2.0/hold 5 was the IS
# winner and is held fixed here.
GRID = {
    "max_hold_days": [5],
    "entry_limit_atr": [0, 0.25, 0.5],   # rest a limit below the close vs buy the open
    "market_filter": [False, True],      # no entries while SPY < its 200-SMA
    "min_atr_pct": [0, 1.5],             # volatility floor: move must clear costs
}

_BARS: pd.DataFrame | None = None


def _init(bars_path: str) -> None:
    global _BARS
    _BARS = pd.read_parquet(bars_path)


def run_combo(job: tuple[dict, str | None, str | None]) -> dict:
    params, start, end = job
    cfg = deepcopy(load_settings().raw)
    for entry in cfg.get("strategies") or [cfg["strategy"]]:
        if entry["name"] == params.get("_strategy", "trend_pullback"):
            entry["params"].update({k: v for k, v in params.items()
                                    if not k.startswith("_")})
    settings = Settings(mode=Mode.BACKTEST, raw=cfg)
    risk = RiskManager(cfg, AuditLog(":memory:"))
    result = BacktestEngine(settings, build_strategy(cfg),
                            risk).run(_BARS, start=start, end=end)
    m = result.metrics
    stress = stress_costs(result.trades, 2.0)
    return {**params, "trades": m["trades"], "sharpe": m.get("sharpe", 0),
            "pf": m.get("profit_factor", 0), "maxdd": m.get("max_drawdown_pct"),
            "ret": m.get("total_return_pct"), "dd_halts": m.get("drawdown_halts"),
            "pf2x": stress["profit_factor"], "ok2x": stress["profitable"]}


def fmt(r: dict) -> str:
    knobs = " ".join(f"{k.replace('_', '')[:9]}={r[k]}" for k in GRID)
    return (f"{knobs} | n={r['trades']:>4} sharpe={r['sharpe']:>5.2f} "
            f"pf={r['pf']:>4.2f} dd={r['maxdd']:>5.2f}% ret={r['ret']:>6.1f}% "
            f"halts={r['dd_halts']} pf@2x={r['pf2x']:>4.2f} "
            f"{'OK' if r['ok2x'] else 'FAIL'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--is-end", default="2021-12-31")
    ap.add_argument("--oos-start", default="2022-01-01")
    ap.add_argument("--workers", type=int, default=5)
    args = ap.parse_args()

    cfg = load_settings().raw
    creds = broker_creds(Mode.PAPER)
    symbols = [s for ss in cfg["universe"]["buckets"].values() for s in ss]
    data = AlpacaData(creds.key_id.get_secret_value(), creds.secret_key.get_secret_value())
    bars = data.daily_bars(symbols, "2016-01-04", pd.Timestamp.today().date().isoformat())
    bars_path = str(Path(tempfile.mkdtemp()) / "bars.parquet")
    bars.to_parquet(bars_path)

    combos = [dict(zip(GRID, values))
              for values in __import__("itertools").product(*GRID.values())]
    print(f"IN-SAMPLE ..{args.is_end}: {len(combos)} combos, {args.workers} workers")
    with Pool(args.workers, initializer=_init, initargs=(bars_path,)) as pool:
        is_rows = pool.map(run_combo, [(c, None, args.is_end) for c in combos])

    is_rows.sort(key=lambda r: (r["ok2x"], r["sharpe"]), reverse=True)
    for r in is_rows:
        print("  " + fmt(r))

    eligible = [r for r in is_rows if r["ok2x"] and r["trades"] >= 150]
    if not eligible:
        print("\nNo combo survives 2x costs with enough trades in-sample. "
              "Structural change needed, not parameters.")
        return
    top = eligible[:3]

    print(f"\nOUT-OF-SAMPLE {args.oos_start}.. (top {len(top)} IS combos, judged once):")
    jobs = [({k: r[k] for k in GRID}, args.oos_start, None) for r in top]
    with Pool(min(args.workers, len(jobs)), initializer=_init,
              initargs=(bars_path,)) as pool:
        oos_rows = pool.map(run_combo, jobs)
    for r in oos_rows:
        print("  " + fmt(r))

    winner = {k: top[0][k] for k in GRID}
    folds = [("2016-01-04", "2018-12-31"), ("2019-01-01", "2021-12-31"),
             ("2022-01-01", "2023-12-31"), ("2024-01-01", None)]
    print(f"\nWALK-FORWARD folds for IS winner {winner}:")
    with Pool(min(args.workers, len(folds)), initializer=_init,
              initargs=(bars_path,)) as pool:
        fold_rows = pool.map(run_combo, [(winner, a, b) for a, b in folds])
    profitable = 0
    for (a, b), r in zip(folds, fold_rows):
        profitable += r["ret"] > 0
        print(f"  {a}..{b or 'now':>10} " + fmt(r))
    print(f"\nprofitable folds: {profitable}/4 (gate needs >= 3)")


if __name__ == "__main__":
    try:
        set_start_method("spawn")
    except RuntimeError:
        pass
    main()
