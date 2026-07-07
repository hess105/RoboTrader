# Risk Management — How the System Protects Capital

This is a narrative walkthrough of the risk layers in `risk/manager.py`,
`risk/kill_switch.py`, `monitoring/health.py`, and `execution/reconciler.py`
— what each one actually does, how they interact, and (specifically, since
it was asked directly) a traced-through answer to *"can the pattern day
trading rule ever be violated, including during a full kill-switch stop."*

For what each config number is called and its default, see
[STRATEGY.md](STRATEGY.md)'s Risk Manager section — this document is the
"how it fits together and why," not another parameter table.

## The layers, in the order a trade actually passes through them

```
Strategy signal
     │
     ▼
1. Position sizing        — how big, if anything, gets bought
     │
     ▼
2. Halt gate               — is trading allowed at all right now
     │
     ▼
3. Reconciliation           — does the journal agree with the broker
     │
     ▼
4. Kill switch              — the one thing that bypasses 1–3 on purpose
     │
     ▼
5. Health monitor           — what happens if the broker connection dies
```

### 1. Position sizing — volatility-normalized, fixed-fractional

Every entry is sized off the distance between its entry price and its
protective stop, not off a fixed share count or a fixed dollar amount:

```
risk_$   = equity × risk_per_trade_pct × vol_scale()
notional = risk_$ × entry_price / (entry_price − stop_price)
```

A wider stop (bigger perceived risk on that specific trade) automatically
buys fewer shares; a tighter stop buys more — the dollar amount actually at
risk if the stop is hit stays roughly constant across very different
setups. That result is then capped, in order, by the position notional
limit, remaining room under gross exposure, and — critically for the PDT
question below — **currently settled cash**. A signal with no stop price at
all is rejected outright; there is no path to an unprotected entry.

### 2. The halt gate — a ranked state machine, not a boolean

`RiskManager.halt` is one of six states, ranked low to high severity:
`none < daily_loss < weekly_loss < reconcile < drawdown < kill_switch`. Two
rules make this simple to reason about even under stress:

- **Halts only escalate.** A new daily-loss halt can't downgrade an active
  drawdown halt. The system can only get more conservative on its own; it
  takes a human to loosen it (`manual_reset`).
- **Two different severities of "halted."** `daily_loss` / `weekly_loss` /
  `reconcile` block *new entries only* — you're not adding risk, but
  existing positions still manage themselves normally (RSI exits, time
  stops, rank-based exits all still fire). `drawdown` and `kill_switch` are
  stricter: they block entries **and** discretionary exits too. Only
  protective stops still fire under those two — the logic being that if
  the circuit breaker tripped, the only trades that should still happen
  are ones that reduce risk, never ones a strategy merely "wants."

`drawdown` is the one that requires a human: it needs `manual_reset` with a
non-empty journaled note before trading resumes, and peak equity used for
future drawdown math gets rebased to the reset-time equity (otherwise the
next tick would just instantly re-trip the same breaker off the old high).
Peak equity itself is restored from journaled equity marks at every engine
restart — you cannot clear a drawdown by restarting the process.

### 3. Reconciliation — the broker is always right about *what*, the journal about *why*

Every startup, every reconnect, and every day at 09:00 ET, the engine
compares what the journal expects to hold against what the broker actually
reports. A mismatch engages the `reconcile` halt (new entries blocked)
until a human looks — the reasoning being that a mismatch means the
journal missed something (a fill during downtime, manual activity in the
same account), and sizing new trades off a belief that might already be
wrong is how small errors become big ones.

### 4. The kill switch — the one thing that bypasses the guards above, on purpose

`risk/kill_switch.py`'s `fire()` does four things in order: engage the
`kill_switch` halt, cancel every open order, call
`broker.close_all_positions()` **directly** — not through
`RiskManager.approve()` — and poll until the broker confirms flat. That
direct broker call is deliberate: an emergency stop that could itself be
blocked by `day_trade_guard`, a bucket cap, or anything else in the normal
approval path would defeat the entire point of having one. The kill switch
has to work regardless of what else is going on.

That bypass is exactly the scenario the second half of this document
verifies isn't a regulatory problem.

### 5. Health monitor — what happens if the broker just goes quiet

Protective stops in this system are **engine-monitored**, not resting at
the broker (Alpaca doesn't support stop orders on fractional shares — see
`monitoring/health.py`). That means an engine/connectivity outage is a real
gap in protection, not just an inconvenience, which is why
`kill_after_disconnect_min` exists: after that many consecutive minutes of
broker-connectivity failure *while holding positions*, the kill switch is
armed to fire the moment connectivity returns — the policy being "if we
couldn't watch the stops, flatten and stop trading rather than resume
blind," not "hope nothing moved while we were out."

---

## Can the Pattern Day Trading rule ever be violated — even during a full kill-switch stop?

Short answer: **no, and not because of a guard that could fail — because
the rule doesn't apply to this account type at all, and the adjacent rule
that *does* apply to cash accounts is prevented by the same settled-cash
check that governs every entry, which the kill switch never touches.**
Here's the traced argument, not just the assertion:

**1. FINRA's Pattern Day Trader rule only applies to margin accounts.**
`config/base.yaml` sets `account_type: cash`. PDT restricts accounts under
$25k equity to 3 day trades per rolling 5 business days *specifically
because margin accounts can use broker-lent capital to over-trade* — a cash
account, funded entirely with the trader's own settled money, is
structurally outside that rule's scope by FINRA's own definition. There is
no code anywhere in this repo that requests margin, checks buying power
beyond cash, or could place a trade the broker would even classify as a
margin day trade — confirmed by grep: no `margin`, no `short`, no
short-selling path exists anywhere in `execution/` or `risk/`.

**2. The rule that *does* apply to a cash account is the Good Faith
Violation / free-riding rule (Reg T / FINRA Rule 4130) — not PDT, but often
confused with it.** A cash account can be restricted to cash-available-only
trading for 90 days after repeated violations of a *different* rule: buying
a security with funds that haven't settled yet (T+1), then selling that
same security before the funds used to buy it settle.

**3. `settled_cash_only` prevents this at the only point it can occur — the
buy.** `risk/manager.py`'s sizing step caps every entry's notional at
`portfolio.settled_cash` when this flag is on (the default). The engine
structurally cannot open a position with money that hasn't settled. If no
position is ever bought with unsettled funds, selling it — same day, next
day, via a strategy exit, or via the kill switch — cannot be a good-faith
violation, because the violation is defined by what funded the *purchase*,
not by how soon the *sale* happens afterward.

**4. The kill switch only ever sells.** `close_all_positions()` cancels
orders and liquidates existing longs; it never opens a new position. There
is no long-only-vs-short question either — the whole system is long-only
(both sleeves; grep confirms no short-side code exists), so "covering a
short" — the other classic way a flatten-everything action could touch
unsettled-fund mechanics — isn't a pathway that exists here.

**Put together: PDT doesn't apply (cash account, no margin, no shorting,
confirmed in code, not just config intent), the adjacent rule that
genuinely does apply to cash accounts is prevented at the buy side by a
check that runs on every single entry regardless of mode, and the kill
switch's bypass of other guards never touches that specific check because
it never buys anything.** This holds during a normal session, and it holds
identically during a full kill-switch stop, because the kill switch's
bypass of `RiskManager.approve()` only skips checks that matter for
*buying* (position caps, bucket caps, day-trade guard) — none of which are
in the causal chain that produces a good-faith violation in the first
place.

### The one adjacent gap worth knowing about (not a day-trading-rule issue)

While tracing this, one real gap surfaced in `_approve_exit()`: the
`day_trade_guard` check has no exception for protective-stop exits, unlike
the halt-block check immediately above it in the same function which
explicitly does (`not signal.reason.startswith("protective")`). Practical
effect: if a position's protective stop is hit the same session it was
opened, `day_trade_guard` defers that exit to the next session — same as
it would a discretionary exit — leaving the position genuinely unprotected
for the rest of that session. This is **not** a day-trading-rule risk (the
argument above doesn't depend on same-day exits happening or not
happening at all); it's a protection-window gap. Documented in detail in
[STRATEGY.md](STRATEGY.md#risk-manager-risk-managerpy--how-a-signal-becomes-an-order)
and left unfixed for now at your call, not mine.

Note also that this gap **cannot** be triggered via the kill switch — the
kill switch bypasses `day_trade_guard` entirely (point 4 above), so it will
flatten a same-day position without hesitation. The gap only applies to a
same-day protective-stop signal going through the *normal* strategy-exit
path, which is a narrower window than "any full stop."

### What to actually verify, periodically

Code review is not the same as proof the mechanism works under real broker
conditions. The project's own standing rule (README) is to drill the kill
switch monthly in paper — both the GUI button and `make kill` / `make
docker-kill` — specifically so "the kill switch bypasses other guards" is
something you've watched happen, not just something written down. An
untested kill switch is decoration; this document explains why the design
is sound, not that a specific deployment is.
