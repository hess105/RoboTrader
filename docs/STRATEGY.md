# Trading Algorithm — How It Works, and What Every Parameter Does

This is a reference for the actual trading logic: what the overnight_effect
strategy does, how the risk manager turns its signals into orders, and what
every tunable number in `config/base.yaml` means. It describes *current
behavior*, derived from the code — if you change a parameter, this doc and
reality will drift apart until someone updates it.

If you're looking for *why* a specific value was chosen, that's in
`config/base.yaml`'s inline comments and `journal/backtests/`, not here.
This doc is "what does this knob do," not "why is it set to 1.5."

**2026-08-23: pivoted from two multi-day swing sleeves (trend_pullback +
momentum_rotation) to a single single-session overnight strategy.** The old
sleeves' code and tests still live in the repo (`strategies/trend_pullback.py`,
`strategies/momentum_rotation.py`) but aren't referenced by `config/base.yaml`
any more. This pivot reset the pipeline to **Gate 1** (`docs/GATES.md`) —
nothing below has been paper- or live-validated yet.

## The big picture

RoboTrader runs a single strategy, **`overnight_effect`**
(`strategies/overnight_effect.py`): buy a diversified basket of liquid
large-cap names near the close, sell the entire basket at the very next
session's open. This targets the well-documented **overnight effect** —
historically, close-to-open returns have accounted for a disproportionate
share of total US equity market return, while intraday (open-to-close)
sessions have been comparatively flat and noisy (see e.g. Lou/Polk/Skouras,
"A Tug of War: Overnight Versus Intraday Expected Returns"). The strategy
is never exposed to a full trading session — it's in the market only
between one day's close and the next day's open.

Stock selection uses an **intraday-reversal score**, computed from a single
completed daily bar (open + close), no intraday data feed required:

```
score = -(close/open - 1) / (atr_pct/100)
```

How many ATRs a name sold off *during its own session today*, sign-flipped
— the biggest intraday washouts rank first, on the bet that part of an
outsized intraday selloff reverses overnight. Only names with `score > 0`
(i.e. names that actually sold off) are ever bought; the top `top_n` by
score, after clearing price/liquidity/volatility floors, become tonight's
basket.

Every strategy class implements the same interface (`strategies/base.py`):
given a read-only view of price history and its own open positions, it
returns a list of buy/sell **Signals** — never orders, never position
sizes. That's deliberate: the *same* strategy code runs unmodified in
backtest, paper, and live, which is what makes a backtest result mean
anything about live behavior.

**`overnight_effect` only ever emits BUY signals.** There is no exit logic
in the strategy at all — `Strategy.overnight_only = True` tells the engine
that every position this strategy opens gets force-closed, unconditionally,
at the very next session's open. That's an engine-level policy
(`backtest/engine.py`'s `_force_close_overnight`, `service/engine.py`'s
`_submit_overnight_exits`), not a strategy decision — there's nothing to
decide, since the hold is always exactly one session by construction.

## Signal → order pipeline

The diagram below (`signal-pipeline.svg`) predates this pivot and still
shows the old dual-sleeve 16:15/09:35 flow — treat it as historical
background on the general shape (signals → risk gate → orders), not as
accurate timing. The current pipeline:

```
15:55 ET  Strategy.on_daily_close()  →  Signal (BUY only, symbol, reason, stop_price)
                ↓
          RiskManager.approve()      →  OrderIntent (notional, sized) or rejected
                ↓
          OrderManager / broker      →  submitted IMMEDIATELY (fills ~at today's close)
                ↓
09:35 ET  every held overnight position force-sold, unconditionally, at the open
```

The risk manager (`risk/manager.py`) is the **only** component allowed to
turn a Signal into an order — it's what sizes every entry and enforces every
limit below. The force-close at 09:35 is the one exception: it bypasses
`risk.approve()` entirely, the same way the kill switch does, because
selling a held position is always risk-reducing, never something a halt
should be able to block.

### Why the entry timing isn't a true close fill

Backtest fills overnight entries at the bar's true, final close — no
approximation, since it's working from completed historical data. Live
can't do that: Alpaca's fractional/notional orders (essential at this
account's <$500 capital) only support plain market orders during regular
hours, not extended-hours or limit orders, so there's no way to submit an
order that waits for the actual 16:00 close and still get filled that
evening. The workaround, `service/engine.py`'s 15:55 ET job: fetch an
intraday snapshot (`AlpacaData.today_snapshot()` — today's real open plus a
latest-trade price standing in for the not-yet-final close), run the
strategy against that as if it were a completed bar, and submit
immediately, five minutes before the close, still within regular hours.
This is a small, structurally-unavoidable **live/backtest timing gap** —
flag it, don't pretend it away. Gate 2's existing "measured slippage ≤
1.5× the backtest cost model" check is exactly the mechanism that will
catch it if it turns out to matter; run that check specifically against
this strategy before any live promotion.

---

## `overnight_effect` (`strategies/overnight_effect.py`)

| Parameter | Starting value | Meaning |
|---|---|---|
| `buckets` | all ten sector buckets | Which `universe.buckets` groups this strategy draws candidates from (resolved to a symbol list at startup — `strategies/__init__.py`). |
| `atr_period` | 14 | Lookback for the ATR used in both the score's denominator and the stop distance. |
| `stop_atr_mult` | 2.5 | `close − stop_atr_mult × ATR` — sizes the position via `risk.approve()`'s entry-to-stop distance. **This is a sizing anchor only, not a monitored exit** (see callout below). |
| `min_atr_pct` | 1.0 | Volatility floor: guards the score's denominator against near-zero-ATR names blowing the score up. |
| `min_price` | 10 | Skip names below this price — avoids microstructure/gap noise inside a much broader universe than the old ETF-only list. |
| `min_avg_dollar_volume` | 50,000,000 | Liquidity floor ($/day), computed per-candidate over `dollar_volume_lookback` trailing days. |
| `dollar_volume_lookback` | 20 | Trading-day window for the liquidity average above. |
| `top_n` | 12 | Nightly basket size — how many of the top-ranked, score-positive candidates to buy. |

All of these are a **starting point set 2026-08-23, not validated** — this
pivot reset the pipeline to Gate 1; real tuning is a
`scripts/sweep_params.py` exercise that hasn't happened yet.

### The stop is a sizing anchor, not a monitored exit

Every other exit mechanism in this codebase (the old sleeves' protective
stops, the kill switch) relies on the engine watching a live price against
a stop level while the market is open. `overnight_effect` positions are
never open during a monitored session at all — bought after the close,
force-sold at the very next open, zero `tick()` calls in between — so the
`stop_price` on each signal can structurally never fire as a real exit. It
exists purely to feed `risk.approve()`'s sizing formula (entry-to-stop
distance). **The actual risk control for this strategy is diversification
across many small positions** (`max_concurrent_positions`,
`max_position_notional_pct`, `max_per_bucket` below), not a stop — this
matters specifically for earnings/news gap risk: a name can gap far past
any stop overnight, and there's no way to protect a single position against
that, only to make sure no single position is big enough to matter. Jeff's
explicit call (2026-08-23): accept that risk via diversification, don't
add an earnings-calendar filter for v1.

---

## Risk Manager (`risk/manager.py`) — how a Signal becomes an order

This is the gate every entry signal passes through, and the *only* thing
that can approve, size, or reject a trade — nothing in the strategy file
does math on dollars or shares.

### Position sizing

```
risk_$   = equity × risk_per_trade_pct  ×  vol_scale()
notional = risk_$ × entry_price / (entry_price − stop_price)
```
...then capped, in order, by `max_position_notional_pct`, remaining room
under `max_gross_exposure_pct`, and (in a cash account) settled cash.
Fractional shares mean any surviving notional above $1 is executable.

| Parameter | Default | Meaning |
|---|---|---|
| `risk_per_trade_pct` | 1.0 | % of equity risked on the distance between entry and stop, per trade. This is the core position-sizing lever — everything else is a cap on top of it. |
| `max_position_notional_pct` | 12 | Hard ceiling: no single position may exceed this % of equity, regardless of what the sizing formula computed. Deliberately tight relative to the old sleeves' 30% — many small positions instead of a few large ones IS the earnings/news gap-risk control for a strategy with zero intraday monitoring (see the stop callout above). |
| `max_gross_exposure_pct` | 100 | Ceiling on total deployed capital across all positions combined (100% = never lever up; this is a cash account). |
| `max_concurrent_positions` | 12 | Maximum number of open positions (plus pending entries) at once — sets the ceiling on the nightly basket size alongside the strategy's own `top_n`. |
| `max_per_bucket` | 3 | Maximum concurrent positions within one correlation bucket (e.g. `technology`, `healthcare`) — caps how concentrated the book gets in one sector even if `max_concurrent_positions` has room. |
| `max_per_bucket_overrides` | `{}` | Per-bucket exceptions to `max_per_bucket` (e.g. `{sectors: 1}`), if a specific bucket needs a tighter cap than the global default. Empty in the validated config. |
| `daily_loss_halt_pct` | 2.5 | Realized + unrealized loss for the day (vs. day-start equity) that halts *new entries* (exits still allowed) for the rest of the session. |
| `weekly_loss_halt_pct` | 5.0 | Same halt, measured against week-start equity. |
| `max_drawdown_halt_pct` | 10.0 | Peak-to-trough equity decline that trips the **full circuit breaker**: blocks new entries *and* discretionary exits (protective stops still fire). Requires a manual reset with a journaled note — a deliberate "pause pending human review," not an auto-resume. |
| `settled_cash_only` | true | Cash-account discipline: never size a position beyond currently-settled cash, even if unsettled proceeds would technically cover it (avoids good-faith violations). |
| `day_trade_guard` | true | Blocks closing a position the same session it was opened, since a cash account has no same-day round-trip allowance worth risking. **Caveat:** unlike the halt-block check immediately above it in `_approve_exit()`, this check has no exception for `signal.reason.startswith("protective")` — a protective-stop exit on a same-day position is deferred to the next session exactly like a discretionary exit would be, leaving the position genuinely unprotected until then. This looks like a gap relative to the stated intent ("protective stops... still fire" — `risk/manager.py`'s own module docstring), not a documented trade-off. |
| `vol_target_pct` | 0 (off) | If set > 0: target annualized realized-vol level. When trailing realized portfolio volatility exceeds this, new-position risk is scaled *down* (never up) — see `vol_scale()` below. |
| `vol_lookback_days` | 20 | Trading-day window used to compute realized volatility for `vol_target_pct`. |

**`vol_scale()`** (only active when `vol_target_pct` is set): computes
trailing realized portfolio volatility over `vol_lookback_days`, and
multiplies new-position risk by `clip(target / realized, 0.25, 1.0)` — it
only ever shrinks position size in loud markets, it never sizes up in quiet
ones, and never shrinks below a 0.25× floor.

### Halts, ranked

`DAILY_LOSS` and `WEEKLY_LOSS` and `RECONCILE` block new entries only —
exits still work. `DRAWDOWN` and `KILL_SWITCH` block entries **and**
discretionary exits (protective stops still rest at the broker/engine and
still fire). Halts only ever escalate within a session — a lower-severity
halt can't downgrade a higher one that's already active. **All of the
above governs entries only** for `overnight_effect`: the 09:35 force-close
of overnight positions never calls `risk.approve()` at all (same bypass the
kill switch uses), so no halt state — including DRAWDOWN — can prevent an
overnight position from being sold at the open.

---

## Universe (`config/base.yaml: universe`)

| Parameter | Default | Meaning |
|---|---|---|
| `buckets` | 10 sector groups, ~114 symbols | The tradeable universe: a curated Dow-30 + Nasdaq-100 snapshot (deduplicated), grouped into approximate sector buckets that `max_per_bucket` caps against. Hand-maintained, not a live index feed — expect membership drift and refresh periodically, same discipline as the prior 26-symbol ETF list it replaced 2026-08-23. |
| `min_price` | 10 | Price floor read by `strategies/overnight_effect.py`'s own filter. |
| `min_avg_dollar_volume` | 50,000,000 | Liquidity floor ($/day), also read directly by the strategy's own filter (per-candidate, over `dollar_volume_lookback` trailing days — see the strategy's param table above). |
| `max_spread_pct` | 0.05 | **Wiring gap, not just documentation:** `risk/manager.py` checks this value under `cfg["risk"]` (`self.rc["max_spread_pct"]`), but it's only defined here under `universe:`. `risk:` has no such key, so the check falls back to its default of `100` — meaning the spread guard does not currently reject anything at the values in this config. Pre-existing gap, not introduced by the overnight pivot — either add `max_spread_pct` under `risk:` too, or move it there, to make this check actually operative. |

## Execution (`config/base.yaml: execution`)

| Parameter | Default | Meaning |
|---|---|---|
| `order_type_entry` / `order_type_exit` / `entry_limit_offset_bps` / `order_timeout_sec` | — | Documented for the old sleeves' resting-limit entries; `overnight_effect` always submits plain notional **market** orders (entries and the forced exit alike) — see the close-fill note above for why (Alpaca's fractional/notional orders don't support limit or extended-hours submission). These values are currently unused by the active strategy. |
| `schedule.signal_time` | 15:55 ET | Matches the hardcoded 15:55 cron job in `service/engine.py`'s `start_scheduler()` — but is still not actually *read* from config (same pre-existing decorative-value pattern as before the pivot; change the job's hardcoded hour/minute, not this key, if you ever need to move it). |
| `schedule.exit_time` | 09:35 ET | Documents the (also hardcoded) `submit_queued` job that force-sells every overnight position at the open. |
| `schedule.reconcile_time` | 09:00 ET | Daily broker-vs-journal reconciliation, in addition to always running at startup. |

## Backtest cost model (`config/base.yaml: backtest`)

These don't affect live trading at all — they model trading friction so a
backtest's numbers aren't fantasy. Relevant when reading Results/Sweep
metrics, not when reasoning about live behavior.

| Parameter | Default | Meaning |
|---|---|---|
| `start` | 2016-01-04 | Earliest date used (Alpaca's free IEX history starts here). |
| `commission_per_share` | 0.0 | Alpaca charges $0 commission on stocks/ETFs. |
| `sec_fee_per_million` | 27.80 | SEC Section 31 regulatory fee on sale proceeds (rate changes ~annually — verify against Alpaca's docs when it resets). |
| `taf_per_share` | 0.000166 | FINRA Trading Activity Fee per share sold (capped at $8.30/trade). |
| `slippage_model` | spread_plus_bps | Fills simulated at mid ± half the spread, plus `extra_slippage_bps` more. |
| `extra_slippage_bps` | 5 | Additional simulated slippage on top of the spread. |
| `partial_fill_prob` | 0.0 | Hook for simulating partial fills; unused at daily-bar position sizes (always fills). |
| `drawdown_reset_sessions` | 10 | Simulated post-mortem cooldown after a backtest circuit-breaker trip (backtest only — live requires an actual manual reset). |

---

## Changing any of this

Every number above lives in `config/base.yaml` (or `config/paper.yaml` /
`config/live.yaml` overrides) — never hardcoded inside a strategy or the
risk manager. That's structural: it's what lets `scripts/sweep_params.py`
and `scripts/sweep_full.py` search this space without touching code, and
what makes "the same code, different config" true across backtest/paper/live.

The rule that matters more than any single parameter: **changing any
strategy or risk parameter resets that strategy to Gate 1** (README Rule 1).
A sweep can find a promising candidate quickly; it doesn't skip the
backtest → paper → live validation cycle that candidate still has to earn.
