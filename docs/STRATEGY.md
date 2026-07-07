# Trading Algorithm — How It Works, and What Every Parameter Does

This is a reference for the actual trading logic: what the two strategy
sleeves do, how the risk manager turns their signals into orders, and what
every tunable number in `config/base.yaml` means. It describes *current
behavior*, derived from the code — if you change a parameter, this doc and
reality will drift apart until someone updates it.

If you're looking for *why* a specific value was chosen, that's in
`config/base.yaml`'s inline comments and `journal/backtests/`, not here.
This doc is "what does this knob do," not "why is it set to 1.5."

## The big picture

RoboTrader runs one portfolio built from two independent strategy
**sleeves** (`strategies/composite.py`):

- **`trend_pullback`** — mean reversion: buys short-term washouts inside a
  longer uptrend, holds days.
- **`momentum_rotation`** — trend following: holds the single strongest
  name/ETF in the universe, rebalanced weekly, holds weeks-to-months.

They're complementary on purpose: pullback earns in choppy uptrends and
goes flat in sustained trends; momentum earns in exactly those sustained
trends (2017 tech, 2022 energy) where pullback sits out. Running both as
one portfolio smooths the equity curve more than either alone.

Every strategy class implements the same interface
(`strategies/base.py`): given a read-only view of price history and its own
open positions, it returns a list of buy/sell **Signals** — never orders,
never position sizes. That's deliberate: the *same* strategy code runs
unmodified in backtest, paper, and live, which is what makes a backtest
result mean anything about live behavior.

## Signal → order pipeline

![Signal-to-order pipeline: daily bars feed both strategy sleeves at 16:15 ET, their signals merge through the composite router into the risk manager, which sizes and gates every trade before submission at the 09:35 ET open. The risk manager also gates the once-a-minute protective stop check and the 09:00 ET reconciliation check — the kill switch is the one trigger that bypasses it entirely, selling directly to the broker.](signal-pipeline.svg)

```
Strategy.on_daily_close()  →  Signal (symbol, side, reason, stop_price)
        ↓
RiskManager.approve()      →  OrderIntent (notional/qty, sized) or rejected
        ↓
OrderManager / broker      →  actual order
```

The risk manager (`risk/manager.py`) is the **only** component allowed to
turn a Signal into an order. A strategy can propose a trade; it cannot
decide how big, and it cannot override a halt. This split is why the risk
parameters below matter as much as the strategy parameters — a strategy
signal with no protective stop, or that would breach a limit, is simply
rejected, no exceptions.

---

## Sleeve 1: Trend Pullback (`strategies/trend_pullback.py`)

**What it does:** long-only mean reversion. Buys a symbol when it's in an
established uptrend (price above its long moving average) but has just sold
off sharply short-term (RSI(2) washout), on the bet that the pullback
resolves back toward the trend. Exits on a bounce (RSI recovers), a time
stop, or the protective stop.

Entry candidates are ranked most-washed-out (lowest RSI) first; if the risk
manager can only approve some of them (position/bucket caps), the deepest
washouts win.

| Parameter | Default | Meaning |
|---|---|---|
| `trend_sma` | 200 | Uptrend filter: only consider longs while price is above this many days' simple moving average. |
| `pullback_rsi_period` | 2 | Lookback for the RSI washout signal — a 2-day RSI reacts fast, by design (this is a short-term timing signal, not a trend indicator). |
| `pullback_rsi_max` | 10 | Entry trigger: RSI(2) must be *below* this (a deep, fast washout) while price is still above `trend_sma`. |
| `exit_rsi_min` | 70 | Exit trigger: close the position once RSI(2) recovers above this (the bounce played out). |
| `max_hold_days` | 5 | Time stop — exit regardless of RSI if a position has been held this many bars. Caps how long capital sits in a trade that never bounces. |
| `atr_period` | 14 | Lookback for the ATR (average true range) volatility measure used for the stop distance and the volatility floor below. |
| `stop_atr_mult` | 2.0 | Protective stop distance: `entry_price − stop_atr_mult × ATR`. Wider multiplier = more room, bigger risk per share. |
| `entry_limit_atr` | 0.25 | If > 0, the entry doesn't buy at the next open — it rests a day-limit order at `close − entry_limit_atr × ATR`, filling only if the washout deepens further. Unfilled limits simply lapse (no cost). Set to `0` to buy the open unconditionally. |
| `min_atr_pct` | 1.5 | Volatility floor: skip a symbol if its ATR is below this % of price. Low-volatility symbols (e.g. utility ETFs) can't clear round-trip trading costs even on a "successful" bounce. |
| `market_filter` | false | Optional regime gate: when true, blocks *all* new entries unless the market proxy (`market_filter_symbol`) is itself above its own `trend_sma`. Off by default — testing showed it cost more (missed bear-rally entries) than it saved. |
| `market_filter_symbol` | SPY | Which symbol acts as the market proxy when `market_filter` is on. |

## Sleeve 2: Momentum Rotation (`strategies/momentum_rotation.py`)

**What it does:** trend following. Once a week (the first trading day of
the ISO week), scores every symbol in the universe by blended medium-term
return, and holds the top-ranked name(s). Exits a holding if it falls out
of the top ranks or its own momentum turns negative — it never holds a
symbol just because everything else is worse (absolute-momentum filter, not
relative-only).

Entries buy at the next open unconditionally — no resting limit, since
momentum is about catching strength, not waiting for a better price.

| Parameter | Default | Meaning |
|---|---|---|
| `buckets` | all four universe buckets | Which `universe.buckets` groups this sleeve is allowed to hold (resolved to a symbol list at startup — see `strategies/__init__.py`). |
| `top_n` | 1 | How many of the top-ranked symbols to hold at once. `1` = maximally concentrated in the single strongest name. |
| `score_fast` | 63 | Trading days (~3 months) for the fast leg of the momentum score. |
| `score_slow` | 126 | Trading days (~6 months) for the slow leg. Score = the *average* of the fast-window and slow-window returns. |
| `min_score` | 0.0 | Absolute-momentum filter: a symbol is never bought (and is exited if held) once its score drops to or below this. Prevents "the least-bad loser" from being long just because it ranks first. |
| `exit_rank` | 3 | A held symbol is sold once it falls out of the top N ranks (by score), even if its score is still positive. |
| `stop_atr_mult` | 3.0 | Protective stop distance in ATRs — wider than the pullback sleeve's, deliberately: trend trades need room to breathe without being stopped out by normal noise. |
| `atr_period` | 14 | Lookback for the ATR used in the stop calculation. |

**Rebalance timing:** evaluated only on the first trading day of each ISO
calendar week — deterministic from the calendar, not from "days since last
rebalance," so backtest and live can never disagree about when a rebalance
happens.

## How the two sleeves share one portfolio (`strategies/composite.py`)

- Each sleeve only ever sees its *own* positions (tracked by which sleeve
  opened them); a sleeve never touches a position it doesn't own.
- Exits always pass through to the risk manager. A **buy** is dropped if
  the symbol is already held by either sleeve, or was already claimed by
  the other sleeve in the same cycle (first sleeve listed in
  `config/base.yaml` wins a same-cycle tie).
- Position/bucket/exposure limits are global across both sleeves — the
  risk manager has no concept of "which sleeve's slot this is," which is
  exactly the point: two sleeves competing for the same limited risk
  budget, not each getting a private one.

---

## Risk Manager (`risk/manager.py`) — how a Signal becomes an order

This is the gate every signal from either sleeve passes through. It's also
the *only* thing that can approve, size, or reject a trade — nothing in
either strategy file does math on dollars or shares.

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
| `max_position_notional_pct` | 30 | Hard ceiling: no single position may exceed this % of equity, regardless of what the sizing formula computed. |
| `max_gross_exposure_pct` | 100 | Ceiling on total deployed capital across all positions combined (100% = never lever up; this is a cash account). |
| `max_concurrent_positions` | 3 | Maximum number of open positions (plus pending entries) across *both* sleeves at once. |
| `max_per_bucket` | 2 | Maximum concurrent positions within one correlation bucket (e.g. `sectors`, `megacap`) — caps how concentrated the book gets in one theme even if `max_concurrent_positions` has room. |
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
halt can't downgrade a higher one that's already active.

---

## Universe (`config/base.yaml: universe`)

| Parameter | Default | Meaning |
|---|---|---|
| `buckets` | 4 groups, 26 symbols | The tradeable universe, grouped into correlation buckets (`index_equity`, `sectors`, `defensive`, `megacap`) that `max_per_bucket` caps against. **Do not widen without a fresh Gate 1 cycle** — a 54-symbol expansion was tested and rejected (deeper drawdowns, a negative 2022 fold) because more names diluted the edge into noisier symbols. |
| `min_avg_dollar_volume` | 50,000,000 | Liquidity floor ($/day) for any symbol to be considered, belt-and-suspenders on top of the curated bucket list. |
| `max_spread_pct` | 0.05 | **Wiring gap, not just documentation:** `risk/manager.py` checks this value under `cfg["risk"]` (`self.rc["max_spread_pct"]`), but it's only defined here under `universe:`. `risk:` has no such key, so the check falls back to its default of `100` — meaning the spread guard does not currently reject anything at the values in this config. Either add `max_spread_pct` under `risk:` too, or move it there, to make this check actually operative. |

## Execution (`config/base.yaml: execution`)

| Parameter | Default | Meaning |
|---|---|---|
| `order_type_entry` | limit | Entries are marketable limit orders (mid + half-spread cap), not naive market orders. |
| `entry_limit_offset_bps` | 5 | How far beyond mid the entry limit is allowed to rest, in basis points. |
| `order_type_exit` | market | Exits are always market orders — slippage on the way out is treated as the cost of certainty; a resting limit that fails to fill on an exit is the worse outcome. |
| `order_timeout_sec` | 60 | An unfilled entry limit is cancelled after this many seconds rather than chased at a worse price. |
| `schedule.signal_time` | 15:50 ET | **Wiring gap, same shape as `max_spread_pct` above:** this value is never read anywhere in `service/engine.py`. The actual signal-computation cron job is hardcoded to `hour=16, minute=15` (`start_scheduler()`) — 25 minutes *after* this config value, and after the 16:00 ET close rather than near it. Functionally harmless today (daily bars are already final well before 16:15), but the config is currently decorative for this one value, not authoritative. |
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
