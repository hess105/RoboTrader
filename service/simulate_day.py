"""Dry-run one or more trading days using the real strategy/risk/data code
paths, with the broker write-side and alert delivery swapped for fakes — so
the simulation is realistic but structurally cannot place a real order or
touch the real audit trail.

Always runs against config/paper.yaml regardless of the live engine's actual
mode. Single-day mode (no start/end) reuses TradingEngine's own
compute_signals/_submit_pending_orders/_sync_fills/_check_stops/daily_summary
against real current data. Multi-day mode walks forward through historical
bars exactly like backtest/engine.py's day loop, but through the SAME
production strategy/risk/order-manager code a live day would run — SimBroker
tracks its own cash/position book (seeded once from the real paper account)
instead of re-reading the real broker each day, so positions/equity/halt
state genuinely carry forward across the simulated range.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

import pandas as pd

from core.models import Order, OrderIntent, OrderStatus, Position, Side
from core.settings import load_settings
from data.view import HistoryView
from execution.alpaca_client import AlpacaExecution
from journal.audit import AuditLog
from monitoring.alerts import Alerter
from service.engine import TradingEngine

SIM_DIR = Path("journal/simulations")
LONG_RUN_WARN_DAYS = 15  # each simulated day pushes real notifications


class SimBroker:
    """Delegates reads to a real (paper) broker until seed_from_real() is
    called; after that it tracks its own cash/position book so a multi-day
    walk-forward evolves independently of the real account. Writes are
    always fabricated — cancel/close_all_positions raise, since nothing in
    the simulation sequence should ever reach them."""

    def __init__(self, real: AlpacaExecution, data):
        self._real = real
        self._data = data
        self._orders: dict[str, Order] = {}
        self._seeded = False
        self._cash = 0.0
        self._positions: dict[str, Position] = {}
        self._mark: dict[str, float] = {}
        self._fill_price: dict[str, float] = {}

    def seed_from_real(self) -> None:
        _equity, cash, _bp = self._real.account_equity()
        self._cash = float(cash)
        for p in self._real.positions():
            self._positions[p.symbol] = Position(
                symbol=p.symbol, qty=p.qty, avg_entry=p.avg_entry,
                stop_price=p.stop_price, opened_at=p.opened_at, bucket=p.bucket)
            self._mark[p.symbol] = float(p.avg_entry)
        self._seeded = True

    def mark(self, symbol: str, price: float) -> None:
        self._mark[symbol] = price

    def set_fill_price(self, symbol: str, price: float) -> None:
        self._fill_price[symbol] = price

    def account_equity(self) -> tuple:
        if not self._seeded:
            return self._real.account_equity()
        mv = sum(float(p.qty) * self._mark.get(p.symbol, float(p.avg_entry))
                 for p in self._positions.values())
        equity = self._cash + mv
        return Decimal(str(equity)), Decimal(str(self._cash)), Decimal(str(self._cash))

    def positions(self) -> list[Position]:
        if not self._seeded:
            return self._real.positions()
        return list(self._positions.values())

    def positions_detail(self) -> list[dict]:
        if not self._seeded:
            return self._real.positions_detail()
        out = []
        for s, p in self._positions.items():
            price = self._mark.get(s, float(p.avg_entry))
            entry = float(p.avg_entry)
            out.append({
                "symbol": s, "qty": float(p.qty), "avg_entry": entry,
                "current_price": price, "market_value": float(p.qty) * price,
                "unrealized_pl": (price - entry) * float(p.qty),
                "unrealized_plpc": (price / entry - 1) * 100 if entry else 0.0,
            })
        return out

    def market_is_open(self) -> bool:
        return True

    def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status is OrderStatus.SUBMITTED]

    def submit(self, intent: OrderIntent) -> Order:
        order = Order(
            client_order_id=intent.client_order_id,
            broker_order_id=f"SIM-{intent.client_order_id}",
            symbol=intent.signal.symbol, side=intent.signal.side,
            status=OrderStatus.SUBMITTED,
            notional=intent.notional, qty=intent.qty,
            submitted_at=datetime.now(timezone.utc),
        )
        self._orders[intent.client_order_id] = order
        return order

    def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        order = self._orders.get(client_order_id)
        if order is None or order.status is not OrderStatus.SUBMITTED:
            return order
        price = self._fill_price.pop(order.symbol, None)
        if price is None:
            price = self._live_quote_price(order.symbol)
        qty = order.qty if order.qty is not None else (
            (order.notional / Decimal(str(price))) if (order.notional and price) else Decimal("0"))
        filled = Order(
            client_order_id=order.client_order_id, broker_order_id=order.broker_order_id,
            symbol=order.symbol, side=order.side,
            status=OrderStatus.FILLED, notional=order.notional, qty=order.qty,
            filled_qty=qty, avg_fill_price=Decimal(str(price)),
            submitted_at=order.submitted_at, updated_at=datetime.now(timezone.utc),
        )
        self._orders[client_order_id] = filled
        if self._seeded and price:
            self._apply_fill(order.symbol, order.side, float(qty), float(price))
        return filled

    def _live_quote_price(self, symbol: str) -> float:
        try:
            bid, ask = self._data.latest_quote(symbol)
            return (bid + ask) / 2
        except Exception:                            # noqa: BLE001 — simulation must not crash
            return 0.0

    def _apply_fill(self, symbol: str, side: Side, qty: float, price: float) -> None:
        if side is Side.BUY:
            pos = self._positions.get(symbol)
            if pos:
                total = float(pos.qty) * float(pos.avg_entry) + qty * price
                new_qty = float(pos.qty) + qty
                pos.qty = Decimal(str(new_qty))
                if new_qty:
                    pos.avg_entry = Decimal(str(total / new_qty))
            else:
                self._positions[symbol] = Position(
                    symbol=symbol, qty=Decimal(str(qty)), avg_entry=Decimal(str(price)),
                    stop_price=None, opened_at=datetime.now(timezone.utc), bucket="")
            self._cash -= qty * price
        else:
            pos = self._positions.get(symbol)
            if pos:
                remaining = float(pos.qty) - qty
                if remaining <= 1e-9:
                    del self._positions[symbol]
                else:
                    pos.qty = Decimal(str(remaining))
            self._cash += qty * price
        self._mark[symbol] = price

    def cancel(self, client_order_id: str) -> None:
        raise NotImplementedError("simulation never cancels a real order")

    def close_all_positions(self) -> list[Order]:
        raise NotImplementedError("simulation never touches the kill switch")


class SimAlerter:
    """Wraps a real Alerter; prefixes every message so it's unmistakably a
    simulated push, and tags it with the simulated day so multi-day runs are
    distinguishable in Telegram/email even though everything arrives within
    seconds of real wall-clock time."""

    def __init__(self, real: Alerter):
        self._real = real
        self.sim_day: str | None = None

    def send(self, severity: str, kind: str, message: str) -> None:
        tag = "[SIMULATION" + (f" {self.sim_day}" if self.sim_day else "") + "]"
        self._real.send(severity, kind, f"{tag} {message}")

    def heartbeat(self) -> None:
        pass  # never ping the real dead-man's switch from a simulation


def run_simulated_day(
    on_progress: Callable[[str], None],
    start: str | None = None,
    end: str | None = None,
) -> dict:
    log: list[dict] = []

    def progress(msg: str) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "message": msg}
        log.append(entry)
        on_progress(f"{entry['ts'][11:19]} {msg}")

    progress("loading paper config…")
    settings = load_settings("config/paper.yaml")

    sim_audit = AuditLog(":memory:")
    sim_alerts = SimAlerter(Alerter(settings.raw))

    engine = TradingEngine(settings, audit=sim_audit, alerts=sim_alerts)
    real_broker: AlpacaExecution = engine.broker
    sim_broker = SimBroker(real_broker, engine.data)
    engine.broker = sim_broker
    engine.om.client = sim_broker

    end_ts = pd.Timestamp(end) if end else pd.Timestamp(datetime.now().date())
    start_ts = pd.Timestamp(start) if start else end_ts
    if start_ts > end_ts:
        raise RuntimeError("start date must be on or before end date")

    fetch_start = (start_ts - pd.Timedelta(days=550)).date().isoformat()
    progress(f"fetching {len(engine.universe)} symbols {fetch_start} .. {end_ts.date()} (cache-first)…")
    bars = engine.data.daily_bars(engine.universe, fetch_start, end_ts.date().isoformat())
    frames = {str(s): df.droplevel(0).sort_index() for s, df in bars.groupby(level=0)}

    all_days = sorted({ts for df in frames.values() for ts in df.index})
    sim_days = [d for d in all_days if start_ts <= d <= end_ts]
    if not sim_days:
        raise RuntimeError("no trading days in the requested range (market holiday/weekend or no data)")
    if len(sim_days) > LONG_RUN_WARN_DAYS:
        progress(f"heads up: {len(sim_days)} simulated days will each push real "
                 f"notifications to Telegram/email")

    progress("seeding starting portfolio from the real paper account…")
    sim_broker.seed_from_real()
    engine.refresh_portfolio()

    equity_curve: list[tuple[str, float]] = []
    for i, day in enumerate(sim_days):
        sim_alerts.sim_day = str(day.date())
        engine.risk.new_session(day)

        for sym, df in frames.items():
            sub = df[df.index <= day]
            if not sub.empty:
                sim_broker.mark(sym, float(sub["close"].iloc[-1]))

        # 1. protective stops against today's low, for positions entered on
        #    or before yesterday (never today's own fresh entries).
        def low_lookup(sym: str, _day=day) -> float | None:
            df = frames.get(sym)
            if df is None or _day not in df.index:
                return None
            return float(df.loc[_day, "low"])

        for sym in engine.portfolio.positions:
            low = low_lookup(sym)
            if low is not None:
                sim_broker.set_fill_price(sym, low)
        progress(f"{day.date()}: checking protective stops…")
        engine._check_stops_at(low_lookup)
        engine._sync_fills()
        engine.refresh_portfolio()

        # 2. new signals as-of today's close, queued for tomorrow's open.
        progress(f"{day.date()}: computing signals…")
        view = HistoryView(frames, day, engine.strategy.warmup_bars())
        engine._evaluate_and_queue(view, day)

        # 3. snapshot equity at today's close, before tomorrow's fills land.
        equity, _cash, _bp = sim_broker.account_equity()
        equity_curve.append((str(day.date()), float(equity)))
        engine.risk.on_mark(day, float(equity))

        # 4. fill today's queued entries at the next trading day's open.
        next_day = sim_days[i + 1] if i + 1 < len(sim_days) else None
        if next_day is not None:
            progress(f"{day.date()}: submitting queued intents for {next_day.date()} open…")
            for sym, df in frames.items():
                if next_day in df.index:
                    sim_broker.set_fill_price(sym, float(df.loc[next_day, "open"]))
            engine._submit_pending_orders()
            engine._sync_fills()
        else:
            progress(f"{day.date()}: last day in range — any queued intents are left "
                     f"unfilled (no next trading day in the fetched data)")
        engine.refresh_portfolio()

    sim_alerts.sim_day = str(sim_days[-1].date())
    progress("sending daily summary…")
    engine.daily_summary()

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    report = {
        "run_id": run_id,
        "start": str(sim_days[0].date()), "end": str(sim_days[-1].date()),
        "days": len(sim_days),
        "equity": equity_curve,
        "orders": sim_audit.orders_recent(500),
        "events": sim_audit.query(None, limit=500),
        "log": log,
    }
    progress("saving results…")
    out_dir = SIM_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(report, indent=2, default=str))
    progress("done")
    return report
