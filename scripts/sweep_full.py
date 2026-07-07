"""Round 4: joint sweep across BOTH sleeves' signal parameters AND
portfolio-construction (allocation/risk) knobs simultaneously — a wider,
higher-dimensional search than sweep_params.py (signals only) or
sweep_allocation.py (allocation only) ran individually.

This is a bigger combinatorial space than a "small grid, every axis
economically motivated" single round, which is exactly the shape of search
that overfits — the caution already baked into every other sweep script in
this repo, and into README Rule 1 ("any strategy parameter change resets
that strategy to Gate 1"). To keep that honest:

  * every axis here already appears individually in sweep_params.py or
    sweep_allocation.py, or is a direct extension of one — no arbitrary
    new knobs invented just for this script
  * the full grid is far too large to run exhaustively, so this RANDOMLY
    SAMPLES --n-samples combos instead of every combination
  * selection happens ONLY on the in-sample window; the winner is judged
    once out-of-sample and across the same 4 walk-forward folds used
    elsewhere, and must survive 2x cost stress
  * a winner from this script is a CANDIDATE, not a result — it still
    needs a fresh `make backtest` Gate 1 run before anything changes in
    config/base.yaml, and a large IS/OOS Sharpe gap is flagged explicitly

The GUI's "Run Sweep" button (Sweep tab) runs this exact same underlying
backtest/sweep.py — this script is just its CLI face.

  python -m scripts.sweep_full                        # 250 random combos
  python -m scripts.sweep_full --n-samples 500 --workers 8
"""
from __future__ import annotations

import argparse
from multiprocessing import set_start_method

from backtest.sweep import fmt, run_sweep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--is-end", default="2021-12-31")
    ap.add_argument("--oos-start", default="2022-01-01")
    ap.add_argument("--n-samples", type=int, default=250)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    print("=" * 70)
    print("JOINT SWEEP — both sleeves' signal params + allocation, searched together.")
    print("A winner here is a CANDIDATE only — still needs a fresh `make backtest`")
    print("Gate 1 run before touching config/base.yaml. See docs/GATES.md.")
    print("=" * 70)

    try:
        out = run_sweep(args.n_samples, args.workers, args.is_end,
                         args.oos_start, args.seed, on_progress=print)
    except RuntimeError:
        raise SystemExit(
            "No paper API credentials found.\n"
            "Run `make keys-paper` (or set ALPACA_PAPER_KEY_ID / "
            "ALPACA_PAPER_SECRET_KEY) first — free Alpaca paper keys are "
            "enough for historical data."
        )

    print(f"\nFull grid: {out['full_grid_size']} combos. Sampled {out['n_samples']} "
          f"(seed={out['seed']}).")
    print(f"\nTop {len(out['is_top'])} in-sample results:")
    for r in out["is_top"]:
        print("  " + fmt(r))

    if not out["eligible"]:
        print("\nNo sampled combo survives 2x costs with enough trades in-sample.")
        print("Widen --n-samples, or this region of the space isn't promising.")
        return

    print(f"\nOUT-OF-SAMPLE {args.oos_start}.. (top 3 IS combos, judged once):")
    for r in out["oos_top"]:
        print("  " + fmt(r))

    print(f"\nWALK-FORWARD folds for IS winner:\n  {out['winner']}")
    for f in out["folds"]:
        print(f"  {f['start']}..{f['end'] or 'now':>10} " + fmt(f))
    print(f"\nprofitable folds: {out['profitable_folds']}/4 (gate needs >= 3)")

    if out["overfit_warning"]:
        print(f"\nWARNING: {out['overfit_warning']}")
        print("Treat this winner with extra skepticism even if it technically passes.")

    print(f"\nSaved to journal/sweeps/{out['run_id']}/results.json")


if __name__ == "__main__":
    try:
        set_start_method("spawn")
    except RuntimeError:
        pass
    main()
