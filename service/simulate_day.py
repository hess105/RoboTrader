"""Dry-run a full trading day using the real strategy/risk/data code paths,
with the broker write-side and alert delivery swapped for fakes — so the
simulation is realistic but structurally cannot place a real order or touch
the real audit trail.

Always runs against config/paper.yaml regardless of the live engine's actual
mode. Reuses TradingEngine's own compute_signals/_submit_pending_orders/
_sync_fills/_check_stops/daily_summary UNMODIFIED: SimBroker fabricates a
SUBMITTED order on submit() and flips it to FILLED on the next lookup, so
_sync_fills() discovers the "fill" on its normal poll and fires the real
fill alert — zero duplicated alerting logic.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from core.models import Order, OrderIntent, OrderStatus, Position
from core.settings import load_settings
from execution.alpaca_client import AlpacaExecution
from journal.audit import AuditLog
from monitoring.alerts import Alerter
from service.engine import TradingEngine


class SimBroker:
    """Delegates reads to a real (paper) broker; fabricates writes."""

    def __init__(self, real: AlpacaExecution, data):
        self._real = real
        self._data = data
        self._orders: dict[str, Order] = {}

    def account_equity(self) -> tuple:
        return self._real.account_equity()

    def positions(self) -> list[Position]:
        return self._real.positions()

    def positions_detail(self) -> list[dict]:
        return self._real.positions_detail()

    def market_is_open(self) -> bool:
        return self._real.market_is_open()

    def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status is OrderStatus.SUBMITTED]

    def submit(self, intent: OrderIntent) -> Order:
        order = Order(
            client_order_id=intent.client_order_id,
            broker_order_id=f"SIM-{intent.client_order_id}",
            symbol=intent.signal.symbol,
            side=intent.signal.side,
            status=OrderStatus.SUBMITTED,
            notional=intent.notional,
            qty=intent.qty,
            submitted_at=datetime.now(timezone.utc),
        )
        self._orders[intent.client_order_id] = order
        return order

    def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        order = self._orders.get(client_order_id)
        if order is None or order.status is not OrderStatus.SUBMITTED:
            return order
        price = self._fill_price(order.symbol)
        qty = order.qty if order.qty is not None else (
            (order.notional / price) if (order.notional and price) else Decimal("0"))
        filled = Order(
            client_order_id=order.client_order_id,
            broker_order_id=order.broker_order_id,
            symbol=order.symbol, side=order.side,
            status=OrderStatus.FILLED,
            notional=order.notional, qty=order.qty,
            filled_qty=qty, avg_fill_price=price,
            submitted_at=order.submitted_at,
            updated_at=datetime.now(timezone.utc),
        )
        self._orders[client_order_id] = filled
        return filled

    def _fill_price(self, symbol: str) -> Decimal:
        try:
            bid, ask = self._data.latest_quote(symbol)
            return Decimal(str((bid + ask) / 2))
        except Exception:                            # noqa: BLE001 — simulation must not crash
            return Decimal("0")

    def cancel(self, client_order_id: str) -> None:
        raise NotImplementedError("simulation never cancels a real order")

    def close_all_positions(self) -> list[Order]:
        raise NotImplementedError("simulation never touches the kill switch")


class SimAlerter:
    """Wraps a real Alerter; prefixes every message so it's unmistakably a
    simulated push, never confusable with a real trading alert."""

    def __init__(self, real: Alerter):
        self._real = real

    def send(self, severity: str, kind: str, message: str) -> None:
        self._real.send(severity, kind, f"[SIMULATION] {message}")

    def heartbeat(self) -> None:
        pass  # never ping the real dead-man's switch from a simulation


def run_simulated_day(on_progress: Callable[[str], None]) -> dict:
    on_progress("loading paper config…")
    settings = load_settings("config/paper.yaml")

    sim_audit = AuditLog(":memory:")
    sim_alerts = SimAlerter(Alerter(settings.raw))

    engine = TradingEngine(settings, audit=sim_audit, alerts=sim_alerts)
    real_broker: AlpacaExecution = engine.broker
    engine.broker = SimBroker(real_broker, engine.data)
    engine.om.client = engine.broker

    on_progress("refreshing portfolio…")
    engine.refresh_portfolio()

    on_progress("computing signals (16:15 close logic)…")
    engine.compute_signals()

    on_progress("submitting queued intents (9:35 open logic)…")
    engine._submit_pending_orders()

    on_progress("syncing simulated fills…")
    engine._sync_fills()

    on_progress("checking protective stops…")
    engine._check_stops()
    engine._sync_fills()

    on_progress("sending daily summary…")
    engine.daily_summary()

    on_progress("done")
    return {
        "orders": sim_audit.orders_recent(200),
        "events": sim_audit.query(None, limit=200),
    }
