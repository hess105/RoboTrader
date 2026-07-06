# Promotion gates — do not skip, do not soften mid-drawdown

Every gate review is journaled: run IDs, metrics, decision, date. If a gate
fails, the strategy goes back one stage; parameters may only be changed at the
backtest stage (any parameter change resets the pipeline to Gate 1).

## Gate 1 — Backtest → Paper
- [ ] ≥ 200 closed trades over ≥ 8 years of data (must span 2018 Q4, 2020 COVID, 2022 bear)
- [ ] Walk-forward: ≥ 4 out-of-sample folds; strategy profitable in ≥ 3 of 4
- [ ] In-sample Sharpe ≥ 1.0; out-of-sample Sharpe ≥ 0.7
      (Sharpe > 2.5 on a daily-bar swing system = suspect overfitting/look-ahead — investigate, don't celebrate)
- [ ] Max drawdown ≤ 15% (breaker is 10%; backtest must show the breaker would rarely fire)
- [ ] Profit factor ≥ 1.3; positive expectancy after costs
- [ ] Edge survives 2× cost model (backtest/metrics.stress_costs still profitable)
- [ ] Look-ahead tripwire test suite green

## Gate 2 — Paper → Live (small size)
- [ ] ≥ 60 trading days of paper AND ≥ 30 closed paper trades, on the exact config intended for live
- [ ] Paper equity curve within the backtest's Monte Carlo 90% band (no "it's different live" excuses)
- [ ] Median realized slippage ≤ 1.5× the backtest cost model; update cost model to measured values and confirm Gate 1 still passes
- [ ] Zero unexplained order errors / duplicate orders / reconcile mismatches in the final 20 sessions
- [ ] Kill switch and scripts/kill.py fired successfully in paper at least twice
- [ ] Alerts verified end-to-end on phone (disconnect drill: kill network mid-session)

## Gate 3 — Small live → Scaled live
Start live at 50% of target risk (0.5%/trade) regardless of gates passed.
- [ ] ≥ 3 calendar months AND ≥ 30 closed live trades at current size
- [ ] Live expectancy within the paper/backtest confidence band; no breaker trips caused by system faults (market-driven trips are acceptable data)
- [ ] Scale rule: +50% of current risk budget per additional month that re-passes the checks above, capped at the config risk targets. Never scale while in drawdown.
- [ ] De-scale rule: 7% live drawdown → drop one size level automatically

## Standing rule (all live stages)
Live peak-to-trough drawdown > 10% → engine auto-halts (risk/manager.py),
positions keep protective stops, and trading stays halted until a written
post-mortem is journaled and the operator manually resets. No same-week
restarts after a drawdown halt.
