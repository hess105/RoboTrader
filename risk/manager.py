"""Risk manager: the only component allowed to turn a Signal into an
OrderIntent. Every check here runs identically in backtest, paper, and live —
that identity is what makes backtest results meaningful for the gates. The
GUI merely displays risk state and can never bypass it.

Sizing (volatility-normalized, fixed-fractional risk):
    risk_$   = equity * risk_per_trade_pct
    notional = risk_$ * price / (price - stop_price)
    capped by max_position_notional_pct, remaining gross exposure, and (in a
    cash account) settled cash. Fractional shares make any surviving notional
    executable down to $1.

Halt semantics:
    DAILY_LOSS / WEEKLY_LOSS / RECONCILE: entries blocked, exits allowed.
    DRAWDOWN / KILL_SWITCH: entries AND discretionary exits blocked;
    protective stops rest at the broker (engine-level in backtest) and still
    fire. DRAWDOWN requires manual_reset with a journaled note — this is the
    standing "pause pending review" rule. Halts only escalate; a lower-rank
    breach never downgrades a higher-rank halt.

Peak equity is restored from journaled equity marks at construction, so a
restart can never reset a drawdown.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from decimal import Decimal

import pandas as pd

from core.models import OrderIntent, Side, Signal, new_client_order_id
from journal.audit import AuditLog


class HaltState:
    NONE = "none"
    DAILY_LOSS = "daily_loss"
    WEEKLY_LOSS = "weekly_loss"
    RECONCILE = "reconcile"
    DRAWDOWN = "drawdown"
    KILL_SWITCH = "kill_switch"


_RANK = {
    HaltState.NONE: 0,
    HaltState.DAILY_LOSS: 1,
    HaltState.WEEKLY_LOSS: 2,
    HaltState.RECONCILE: 3,
    HaltState.DRAWDOWN: 4,
    HaltState.KILL_SWITCH: 5,
}
_BLOCKS_EXITS = (HaltState.DRAWDOWN, HaltState.KILL_SWITCH)
_WARN_FRACTION = 0.8


class RiskManager:
    def __init__(self, cfg: dict, audit: AuditLog | None = None, alerts=None):
        self.rc = cfg["risk"]
        self.buckets = {
            s: b
            for b, syms in cfg.get("universe", {}).get("buckets", {}).items()
            for s in syms
        }
        self.audit = audit or AuditLog(":memory:")
        self.alerts = alerts
        self.halt = HaltState.NONE
        self.drawdown_trips = 0
        self.peak_equity: float | None = self.audit.max_equity()
        self._day_start: float | None = None
        self._week_start: float | None = None
        self._week_id: tuple | None = None
        self._warned: set[tuple] = set()
        self.last: dict = {}
        # Volatility targeting: daily closing equity, restart-proof via the
        # journal's equity marks (date -> last equity of that date).
        self._daily_equity: OrderedDict[str, float] = OrderedDict(
            self.audit.daily_equity(int(self.rc.get("vol_lookback_days", 20)) + 5))

    # --- session / clock ---

    def new_session(self, ts) -> None:
        ts = pd.Timestamp(ts)
        self._day_start = None
        if self.halt == HaltState.DAILY_LOSS:
            self._set_halt(HaltState.NONE, "new session", force=True)
        week = tuple(ts.isocalendar())[:2]
        if week != self._week_id:
            self._week_id = week
            self._week_start = None
            if self.halt == HaltState.WEEKLY_LOSS:
                self._set_halt(HaltState.NONE, "new week", force=True)

    def on_mark(self, ts, equity: float) -> None:
        equity = float(equity)
        if self._day_start is None:
            self._day_start = equity
        if self._week_start is None:
            self._week_start = equity
        self.peak_equity = max(self.peak_equity or equity, equity)
        self.audit.event("equity_mark", price=equity, detail=str(ts))
        day_key = str(pd.Timestamp(ts).date())
        self._daily_equity[day_key] = equity
        while len(self._daily_equity) > int(self.rc.get("vol_lookback_days", 20)) + 5:
            self._daily_equity.popitem(last=False)

        day_loss = (self._day_start - equity) / self._day_start * 100
        week_loss = (self._week_start - equity) / self._week_start * 100
        drawdown = (self.peak_equity - equity) / self.peak_equity * 100
        self.last = {"equity": equity, "day_loss_pct": day_loss,
                     "week_loss_pct": week_loss, "drawdown_pct": drawdown}

        for key, state, value in (
            ("max_drawdown_halt_pct", HaltState.DRAWDOWN, drawdown),
            ("weekly_loss_halt_pct", HaltState.WEEKLY_LOSS, week_loss),
            ("daily_loss_halt_pct", HaltState.DAILY_LOSS, day_loss),
        ):
            limit = float(self.rc[key])
            if value >= limit:
                self._set_halt(state, f"{key}: {value:.2f}% >= {limit}%")
            elif value >= _WARN_FRACTION * limit:
                mark = (key, pd.Timestamp(ts).date())
                if mark not in self._warned:
                    self._warned.add(mark)
                    self.audit.event("risk_warning", reason=f"{key} at {value:.2f}% of {limit}% limit")
                    self._alert("WARN", f"{key} at {value:.2f}% (limit {limit}%)")

    # --- the gate ---

    def approve(self, signal: Signal, portfolio, price: float,
                spread_pct: float | None = None) -> OrderIntent | None:
        sym = signal.symbol
        if signal.side is Side.SELL:
            return self._approve_exit(signal, portfolio)

        if self.halt != HaltState.NONE:
            return self._reject(signal, f"entries halted: {self.halt}")
        pending = getattr(portfolio, "pending_entries", set())
        if len(portfolio.positions) + len(pending) >= int(self.rc["max_concurrent_positions"]):
            return self._reject(signal, "max concurrent positions reached")
        bucket = self.buckets.get(sym, "other")
        bucket_count = sum(1 for p in portfolio.positions.values() if p.bucket == bucket)
        bucket_count += sum(1 for s in pending if self.buckets.get(s, "other") == bucket)
        bucket_cap = int(self.rc.get("max_per_bucket_overrides", {})
                         .get(bucket, self.rc["max_per_bucket"]))
        if bucket_count >= bucket_cap:
            return self._reject(signal, f"bucket '{bucket}' at cap {bucket_cap}")
        if spread_pct is not None and spread_pct > float(self.rc.get("max_spread_pct", 100)):
            return self._reject(signal, f"spread {spread_pct:.3f}% too wide")
        if signal.stop_price is None:
            return self._reject(signal, "entry without protective stop")
        # Size against the expected entry: the limit price for resting-limit
        # entries, else the reference price.
        entry_est = float(signal.limit_price) if signal.limit_price is not None else price
        stop_dist = entry_est - float(signal.stop_price)
        if stop_dist <= 0:
            return self._reject(signal, "stop at or above entry price")

        equity = float(portfolio.equity)
        risk_dollars = equity * float(self.rc["risk_per_trade_pct"]) / 100
        risk_dollars *= self.vol_scale()
        notional = risk_dollars * entry_est / stop_dist
        notional = min(notional, float(self.rc["max_position_notional_pct"]) / 100 * equity)
        max_gross = float(self.rc["max_gross_exposure_pct"]) / 100 * equity
        notional = min(notional, max_gross - float(portfolio.gross_exposure))
        if self.rc.get("settled_cash_only"):
            notional = min(notional, float(portfolio.settled_cash))
        if notional < 1.0:
            return self._reject(signal, "notional below $1 minimum after caps")

        intent = OrderIntent(
            client_order_id=new_client_order_id(signal.strategy, sym),
            signal=signal,
            notional=Decimal(str(round(notional, 2))),
            qty=None,
            limit_price=signal.limit_price,
        )
        self.audit.event("risk_approve", symbol=sym, side="buy",
                         price=entry_est, qty=notional / entry_est,
                         order_id=intent.client_order_id, reason=signal.reason)
        return intent

    def _approve_exit(self, signal: Signal, portfolio) -> OrderIntent | None:
        sym = signal.symbol
        pos = portfolio.positions.get(sym)
        if pos is None:
            return self._reject(signal, "exit for symbol not held")
        if self.halt in _BLOCKS_EXITS and not signal.reason.startswith("protective"):
            return self._reject(signal, f"halted ({self.halt}); protective stops remain at broker")
        if self.rc.get("day_trade_guard") and sym in portfolio.bought_today:
            return self._reject(signal, "day-trade guard: bought today, exit deferred to next session")
        intent = OrderIntent(
            client_order_id=new_client_order_id(signal.strategy, sym),
            signal=signal, notional=None, qty=pos.qty, limit_price=None,
        )
        self.audit.event("risk_approve", symbol=sym, side="sell",
                         qty=float(pos.qty), order_id=intent.client_order_id,
                         reason=signal.reason)
        return intent

    def vol_scale(self) -> float:
        """Volatility targeting: shrink new-position risk when realized
        portfolio vol runs above target. scale = clip(target/realized,
        0.25, 1.0) — never sizes UP in quiet markets, only de-risks loud
        ones. Off when vol_target_pct is 0/unset."""
        target = float(self.rc.get("vol_target_pct", 0) or 0)
        if not target:
            return 1.0
        lookback = int(self.rc.get("vol_lookback_days", 20))
        eq = list(self._daily_equity.values())[-(lookback + 1):]
        if len(eq) < 10:
            return 1.0
        rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
        realized = math.sqrt(var) * math.sqrt(252) * 100
        if realized <= 0:
            return 1.0
        return float(min(1.0, max(0.25, target / realized)))

    # --- operator actions / plumbing ---

    def engage(self, state: str, reason: str) -> None:
        """System-initiated halt (kill switch, reconcile mismatch). Kill switch
        always takes over; others follow normal escalation-only ranking."""
        self._set_halt(state, reason, force=(state == HaltState.KILL_SWITCH))

    def manual_reset(self, operator_note: str) -> None:
        if not operator_note.strip():
            raise ValueError("A written reason is required to reset a halt.")
        # Rebase the peak to current equity: without this, the next mark is
        # still >limit% below the old peak and instantly re-trips the breaker.
        # Journaled so a restart restores the rebased peak, not the old high.
        equity = self.last.get("equity")
        if self.halt == HaltState.DRAWDOWN and equity is not None:
            self.peak_equity = equity
            self.audit.event("peak_rebase", price=equity, reason=operator_note)
        self._set_halt(HaltState.NONE, f"manual reset: {operator_note}", force=True)

    def status(self) -> dict:
        limits = {k: self.rc[k] for k in
                  ("daily_loss_halt_pct", "weekly_loss_halt_pct", "max_drawdown_halt_pct")}
        return {"halt": self.halt, "peak_equity": self.peak_equity,
                "limits": limits, **self.last}

    def _set_halt(self, state: str, reason: str, force: bool = False) -> None:
        if not force and _RANK[state] <= _RANK[self.halt]:
            return
        self.halt = state
        if state == HaltState.DRAWDOWN:
            self.drawdown_trips += 1
        self.audit.event("halt_change", reason=reason, detail=state)
        if state != HaltState.NONE:
            self._alert("CRIT", f"HALT -> {state}: {reason}")

    def _reject(self, signal: Signal, reason: str) -> None:
        self.audit.event("risk_reject", symbol=signal.symbol,
                         side=signal.side.value, reason=reason, detail=signal.reason)
        return None

    def _alert(self, severity: str, message: str) -> None:
        if self.alerts is not None:
            self.alerts.send(severity, "risk", message)
