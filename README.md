# RoboTrader

Personal algorithmic trading system with a strict gated pipeline:
**backtest → paper → small live → scaled live** (numeric criteria in
[docs/GATES.md](docs/GATES.md)). Swing strategy (2–10 day holds) on liquid US
ETFs + mega-caps, Alpaca brokerage, cash account, conservative risk
(1%/trade, 10% drawdown circuit breaker).

## How to run

Everything runs through `make` (see the [Makefile](Makefile)):

```
make install      # create .venv and install dependencies
make keys-paper   # store Alpaca paper API keys in the OS keychain
make test         # run the test suite
make backtest     # gate-qualifying backtest (writes journal/backtests/<run_id>/)
make paper        # start the engine daemon in PAPER mode (the default, always)
make gui          # start the web dashboard (client of the engine API)
make kill         # kill switch: flatten all positions, halt the engine
make live         # start the engine in LIVE mode — guarded, see Rules below
make tax YEAR=YYYY  # export realized gains/losses CSV (Form 8949 layout)
```

> **Current status:** all layers are implemented and tested — data, strategy,
> backtester, risk manager, execution (idempotent orders, reconciler, kill
> switch), monitoring/alerts, tax ledger, the engine daemon, and the React
> dashboard. `make paper` starts the engine + GUI at http://127.0.0.1:8765.
> **Gate 1: PASSED** (run 20260706-151928). Two sleeves as one portfolio
> (trend_pullback + momentum_rotation via strategies/composite.py), 2016-2026:
> 327 trades, Sharpe 1.09 (IS 1.14 / OOS 1.08), PF 2.10, max DD 12.1%,
> +179% (CAGR 10.3%), survives 2x costs (PF 1.93), 4/4 walk-forward folds
> profitable with fold Sharpe >= 1.0. Known cost of the allocation choice:
> deeper drawdowns (12% vs 8%) and the 10% breaker pausing ~once per 5 years.
> Next stage: paper trading (Gate 2 — >= 60 trading days AND >= 30 trades on
> this exact config). Nothing gets pointed at real money until Gate 2 passes.

## Rules

These are standing rules, not suggestions. They exist because every one of
them is cheaper than the failure it prevents.

1. **Never skip or soften a gate.** Promotion criteria live in
   [docs/GATES.md](docs/GATES.md) and were set before seeing results —
   changing them mid-drawdown is how accounts die. Any strategy parameter
   change resets that strategy to Gate 1 (backtest).
2. **Mode defaults to paper.** Live requires explicitly starting the engine
   with `config/live.yaml` **and** typing the confirmation phrase on the
   engine's terminal. There is no GUI toggle for live mode, by design.
3. **The engine owns all risk logic.** The GUI is a disposable client; it
   never sizes, approves, or blocks anything. If a risk check would have to
   live in the GUI to work, the design is wrong — move it to the engine.
4. **Only the risk manager turns signals into orders.** Strategies emit
   `Signal`s with reasons; sizing and every pre-trade check happen in
   `risk/manager.py`. No exceptions, including "temporary" manual orders.
5. **Protective stops rest at the broker,** never only in the process, so
   engine downtime never means unprotected positions.
6. **A 10% live drawdown halts everything** pending a written, journaled
   post-mortem. No same-week restarts.
7. **Drill the kill switch monthly in paper** — both the GUI button and
   `make kill`. An untested kill switch is decoration.
8. **Secrets never touch the repo, logs, or config files.** Keys go in the
   OS keychain via `make keys-paper` / `make keys-live`. Live keys aren't
   even created until the paper→live gate is passed.
9. **Cash account discipline:** only buy with settled funds (T+1) — the
   `settled_cash_only` check enforces this; don't disable it.
10. **Every decision is journaled.** If an action can't be expressed through
    the audited path (engine API or CLI), don't take it.

## Build order

Implement in this sequence — each step is testable before the next starts:

1. **Data layer** — `data/cache.py`, then `data/alpaca_data.py`
   (daily bars through the parquet cache; verify against a known symbol).
2. **Indicators + strategy** — `strategies/indicators.py` (done),
   then `strategies/trend_pullback.py` signal logic.
3. **Backtester** — `backtest/costs.py` (done), `backtest/engine.py`,
   `backtest/metrics.py`; get `tests/test_backtest_no_lookahead.py` green.
4. **Run Gate 1** — `make backtest`, walk-forward folds, review against
   docs/GATES.md. Iterate on the strategy *here*, not later.
5. **Risk manager** — `risk/manager.py` with `tests/test_risk_manager.py`
   green before anything can place an order.
6. **Execution** — `execution/alpaca_client.py`, `order_manager.py`
   (idempotency test green), `reconciler.py`.
7. **Engine daemon** — `service/engine.py` scheduler + startup sequence,
   `service/api.py`; then `risk/kill_switch.py`, `monitoring/`,
   `journal/audit.py` wiring.
8. **GUI** — `make gui-init` then the screens per [gui/README.md](gui/README.md).
9. **Paper trade ≥ 60 days** — Gate 2. Only then `make keys-live`.

## Prompt

```
Refined prompt for Fable (with GUI section added):
Act as a senior quantitative developer and trading systems architect. I want to build a personal algorithmic trading system in Python that starts with a small live account (low hundreds to low thousands of dollars) and progresses through a strict, gated pipeline: backtest → paper trade → small-size live trade → scaled live trade. Do not skip gates. Build this out in stages, asking me clarifying questions about risk tolerance, timeframe (intraday/swing/longer-hold), and asset universe before finalizing strategy parameters.
1. Brokerage & compliance Compare 2–3 brokerages offering both a free paper trading API and a live trading API with the same interface (so code doesn't change between paper and live), commission-free equity trading, and fractional shares. For each, note: API stability/docs quality, data latency, rate limits, order types supported, and how they handle margin/day-trading rules. Explain the FINRA Pattern Day Trader rule (accounts under $25k are restricted to 3 day trades per rolling 5 business days) and how the strategy/account structure should account for this. Recommend one.
2. Software suite architecture Design the system with clearly separated layers:
* Data layer: historical + real-time market data ingestion, cached locally
* Strategy engine: modular, swappable strategy classes with a common interface
* Backtesting engine: realistic commissions, slippage, bid/ask spread, partial fills, and look-ahead-bias prevention
* Paper execution layer: same order interface as live, for forward-testing
* Live execution layer: order management, idempotent order submission (no duplicate orders on retry), reconciliation between local state and broker state
* Risk management module: position sizing, per-trade risk cap, daily/weekly loss limits, max drawdown circuit breaker that halts trading automatically
* Secrets management: API keys stored in environment variables or a secrets manager, never hardcoded or logged
* Monitoring/alerting: real-time P&L, open positions, error alerts (e.g., email/SMS) if the system disconnects, an order fails, or a loss limit is breached
* Audit logging: every order, fill, and decision logged with timestamps for tax reporting and post-mortem analysis
* Config system: capital, risk %, tickers, timeframe, and mode (paper/live) all externally configurable, with mode defaulting to paper
3. Trading algorithm Propose a strategy realistic for a small account. Cover:
* Signal logic and why it fits small-capital constraints (liquidity needs, avoiding strategies that lose edge to commissions/spread at small size)
* Position sizing (fixed-fractional or volatility-based, e.g., ATR-based)
* Risk controls: max % risk per trade, daily loss limit that halts trading, max concurrent positions, correlation limits across positions
* Known failure modes (overfitting, regime change, low liquidity, slippage exceeding backtest assumptions) and how the system detects and responds to them automatically
4. Validation and go-live gates Define explicit, numeric criteria required to pass each stage:
* Backtest → Paper: minimum number of backtested trades, acceptable Sharpe ratio range, max historical drawdown
* Paper → Live (small size): minimum paper trading duration (e.g., number of trading days/trades), paper performance thresholds that must be met, confirmation that paper fills reasonably match expected slippage
* Small live → Scaled live: minimum live track record before increasing position size, and a rule for how much to scale up per milestone
* A standing rule: any live drawdown beyond X% pauses the system pending manual review
5. Live-money operational requirements Outline what changes operationally once real money is involved:
* How to securely handle broker API keys and authentication
* Reconciliation checks between what the system thinks it holds and what the broker confirms it holds, run on every startup
* A manual "kill switch" to flatten all positions and halt trading immediately
* Tax record-keeping needs (cost basis tracking, realized gains/losses export) for U.S. tax reporting
* Handling broker downtime, partial fills, and rejected orders gracefully without duplicating or losing orders
6. GUI — primary control interface This GUI is how I will run and monitor the system day-to-day, so it needs to be stable, clear under stress, and hard to misuse. Recommend between a PyQt5/PySide6 desktop app (direct local process control, no server needed, best for a single-machine setup) and a web-based dashboard (Flask/FastAPI backend + React or Streamlit/Dash frontend, better if I want remote/mobile access) — give a clear recommendation based on the tradeoffs. Design the interface with these screens/panels:
* Mode indicator: always-visible, unmissable banner showing PAPER or LIVE mode, with a distinct color scheme for each (e.g., live mode uses a persistent red/amber theme) so it's never ambiguous which mode is active
* Dashboard/home: real-time account equity, day P&L, open positions with unrealized P&L, connection status to broker and data feed
* Strategy control panel: start/stop/pause controls per strategy, current config parameters displayed (not just editable — visible), with confirmation dialogs before any change that affects live trading
* Order/trade blotter: live-updating table of orders and fills, with status (pending/filled/rejected/cancelled)
* Risk panel: current drawdown vs. limit, daily loss vs. limit, visually prominent warning state as limits are approached (not just at breach)
* Kill switch: a single, physically separated, hard-to-misclick button that flattens all positions and halts the system, with a mandatory confirmation step
* Backtest/paper results viewer: equity curve, drawdown chart, trade log, key metrics (Sharpe, win rate, avg win/loss) for reviewing a strategy before promoting it to the next gate
* Logs/audit view: searchable/filterable log of every system decision and order for post-mortem review
* Settings: broker API key entry (masked, never displayed in plaintext after entry), mode switch (paper/live) that requires explicit confirmation and re-entry of a safety phrase to switch into live mode Also specify: how the GUI communicates with the execution engine (e.g., local IPC, REST calls to a background service) so the GUI freezing or crashing does not stop risk checks or the kill switch from functioning, and how the GUI stays responsive during live market data streaming.
7. Tech stack and scaffold Recommend Python libraries (e.g., pandas, backtrader or vectorbt for backtesting, the chosen broker's official SDK, APScheduler or similar for job scheduling, PyQt5/PySide6 or Flask+React for the GUI per the recommendation above) and give me a full project file/folder scaffold, including config/, strategies/, backtest/, execution/, risk/, gui/, and logs/ directories, so I can start implementing against it directly.

One architectural point worth calling out explicitly since it's in the prompt but easy to gloss over: the GUI should never be the thing enforcing risk limits or holding the kill switch logic — that needs to live in a background process/engine that keeps running (and keeps protecting your capital) even if the GUI window crashes or freezes. 
```