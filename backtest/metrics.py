"""Gate metrics computed identically for backtest, paper, and live records,
so stage comparisons are apples-to-apples. Thresholds live in docs/GATES.md.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute_metrics(equity_curve: pd.Series, trades: list) -> dict:
    eq = pd.Series(equity_curve).astype(float)
    m: dict = {"trades": len(trades)}
    if len(eq) < 2:
        m["note"] = "insufficient equity history"
        return m

    rets = eq.pct_change().dropna()
    years = len(eq) / 252
    m["start_equity"] = round(eq.iloc[0], 2)
    m["end_equity"] = round(eq.iloc[-1], 2)
    m["total_return_pct"] = round((eq.iloc[-1] / eq.iloc[0] - 1) * 100, 2)
    m["cagr_pct"] = round(((eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1) * 100, 2) if years > 0 else None
    m["sharpe"] = round(float(rets.mean() / rets.std() * np.sqrt(252)), 2) if rets.std() > 0 else 0.0
    m["max_drawdown_pct"] = round(abs(float((eq / eq.cummax() - 1).min())) * 100, 2)

    if trades:
        pnls = [t.pnl for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gross_loss = abs(sum(losses))
        m["win_rate_pct"] = round(len(wins) / len(pnls) * 100, 1)
        m["profit_factor"] = round(sum(wins) / gross_loss, 2) if gross_loss > 0 else float("inf")
        m["avg_win"] = round(np.mean(wins), 2) if wins else 0.0
        m["avg_loss"] = round(np.mean(losses), 2) if losses else 0.0
        m["expectancy"] = round(np.mean(pnls), 4)
        m["avg_bars_held"] = round(np.mean([t.bars_held for t in trades]), 1)
        in_market = {d for t in trades for d in eq.index if t.entry_ts <= d <= t.exit_ts}
        m["exposure_pct"] = round(len(in_market) / len(eq) * 100, 1)
    return m


def stress_costs(trades: list, cost_multiplier: float = 2.0,
                 base_cost_bps_per_side: float = 7.5) -> dict:
    """Re-price every trade with multiplied friction (gate requirement:
    the edge must survive 2x assumed costs). base = half-spread + slippage."""
    extra = base_cost_bps_per_side * (cost_multiplier - 1) / 1e4
    adj = [t.pnl - (t.entry_price + t.exit_price) * t.qty * extra for t in trades]
    wins = [p for p in adj if p > 0]
    losses = [p for p in adj if p <= 0]
    gross_loss = abs(sum(losses))
    return {
        "cost_multiplier": cost_multiplier,
        "total_pnl": round(sum(adj), 2),
        "profit_factor": round(sum(wins) / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "win_rate_pct": round(len(wins) / len(adj) * 100, 1) if adj else 0.0,
        "profitable": sum(adj) > 0,
    }
