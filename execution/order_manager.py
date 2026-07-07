"""Order lifecycle with idempotent submission. The invariant:

  An OrderIntent is journaled (status pending_submit) BEFORE the first submit
  attempt. On any error/timeout, recovery queries the broker BY
  client_order_id:
    - broker knows it  -> adopt broker state, do NOT resubmit
    - broker doesn't   -> safe to resubmit the SAME client_order_id
  A new client_order_id is never generated for a retry, and the broker
  dedupes on it — so a duplicate order is impossible even if the process
  dies mid-submit. Startup replays all pending_submit rows through
  recover_pending() before any new trading begins.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from core.models import Fill, Order, OrderIntent, OrderStatus, Side, Signal
from execution.broker import ExecutionClient
from journal.audit import AuditLog


class OrderManager:
    def __init__(self, client: ExecutionClient, audit: AuditLog, alerts=None):
        self.client = client
        self.audit = audit
        self.alerts = alerts

    def execute(self, intent: OrderIntent) -> Order | None:
        cid = intent.client_order_id
        self.audit.upsert_order(
            cid, symbol=intent.signal.symbol, side=intent.signal.side.value,
            notional=float(intent.notional) if intent.notional else None,
            qty=float(intent.qty) if intent.qty else None,
            status="pending_submit",
        )
        self.audit.event("order_pending", symbol=intent.signal.symbol,
                         side=intent.signal.side.value, order_id=cid,
                         reason=intent.signal.reason)
        try:
            order = self.client.submit(intent)
        except Exception as exc:
            self.audit.event("order_submit_error", order_id=cid, reason=str(exc))
            order = self._resolve(intent)
        if order is not None:
            self._record(order)
        return order

    def recover_pending(self) -> list[Order]:
        """Startup: resolve every journaled pending_submit against the broker."""
        recovered = []
        for row in self.audit.pending_orders():
            cid = row["client_order_id"]
            existing = self.client.get_order_by_client_id(cid)
            if existing is not None:
                self.audit.event("order_adopted", order_id=cid,
                                 reason="found at broker during recovery")
                self._record(existing)
                recovered.append(existing)
                continue
            intent = OrderIntent(
                client_order_id=cid,
                signal=Signal(strategy="recovery", symbol=row["symbol"],
                              side=Side(row["side"]),
                              reason="resubmitted pending_submit after restart",
                              stop_price=None, ts=datetime.now(timezone.utc)),
                notional=Decimal(str(row["notional"])) if row["notional"] else None,
                qty=Decimal(str(row["qty"])) if row["qty"] else None,
                limit_price=None,
            )
            order = self.client.submit(intent)
            self._record(order)
            recovered.append(order)
        return recovered

    def on_fill(self, fill: Fill) -> None:
        self.audit.event("fill", symbol=fill.symbol, side=fill.side.value,
                         qty=float(fill.qty), price=float(fill.price),
                         order_id=fill.broker_order_id, detail=str(fill.ts))
        if self.alerts is not None:
            self.alerts.send("INFO", "fill",
                             f"filled {fill.side.value} {float(fill.qty):g} "
                             f"{fill.symbol} @ {float(fill.price):.2f}")

    def _resolve(self, intent: OrderIntent) -> Order | None:
        """Error path: broker state decides whether resubmitting is safe."""
        existing = self.client.get_order_by_client_id(intent.client_order_id)
        if existing is not None:
            self.audit.event("order_adopted", order_id=intent.client_order_id,
                             reason="submit errored but broker had accepted it")
            return existing
        try:
            return self.client.submit(intent)       # same client_order_id
        except Exception as exc:                     # stays pending_submit for
            self.audit.event("order_submit_error",   # recover_pending() later
                             order_id=intent.client_order_id, reason=str(exc))
            if self.alerts is not None:
                self.alerts.send("CRIT", "order",
                                 f"submit failed twice for {intent.client_order_id}: {exc}")
            return None

    def record(self, order: Order) -> None:
        """Public: journal a broker-reported state change (used by fill sync)."""
        self._record(order)

    def _record(self, order: Order) -> None:
        self.audit.upsert_order(order.client_order_id,
                                broker_order_id=order.broker_order_id,
                                status=order.status.value)
        self.audit.event(f"order_{order.status.value}", symbol=order.symbol,
                         side=order.side.value, order_id=order.client_order_id)
        if self.alerts is not None and order.status in (OrderStatus.REJECTED, OrderStatus.EXPIRED):
            self.alerts.send("WARN", "order",
                             f"{order.status.value} {order.side.value} {order.symbol}")
