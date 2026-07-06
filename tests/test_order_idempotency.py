"""Order idempotency: timeout-after-submit (ack lost), crash-before-ack, and
double recovery. The broker mock rejects duplicate client_order_ids exactly
like Alpaca does — so these tests prove one intent can never become two
orders, whichever moment the process dies.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from core.models import Order, OrderIntent, OrderStatus, Side, Signal, new_client_order_id
from execution.order_manager import OrderManager
from journal.audit import AuditLog


class FakeBroker:
    def __init__(self):
        self.orders: dict[str, Order] = {}
        self.submit_calls = 0
        self.ack_lost_next = False

    def submit(self, intent: OrderIntent) -> Order:
        self.submit_calls += 1
        cid = intent.client_order_id
        if cid in self.orders:
            raise ValueError(f"duplicate client_order_id {cid}")  # Alpaca behavior
        order = Order(
            client_order_id=cid, broker_order_id=f"b-{len(self.orders) + 1}",
            symbol=intent.signal.symbol, side=intent.signal.side,
            status=OrderStatus.SUBMITTED,
            notional=intent.notional, qty=intent.qty,
        )
        self.orders[cid] = order
        if self.ack_lost_next:          # broker accepted it, but the ack never arrived
            self.ack_lost_next = False
            raise TimeoutError("ack lost")
        return order

    def get_order_by_client_id(self, cid: str) -> Order | None:
        return self.orders.get(cid)


def make_intent() -> OrderIntent:
    signal = Signal("test", "SPY", Side.BUY, "unit test", Decimal("95"),
                    datetime.now(timezone.utc))
    return OrderIntent(client_order_id=new_client_order_id("test", "SPY"),
                       signal=signal, notional=Decimal("100"), qty=None,
                       limit_price=None)


def test_clean_submit_records_order():
    broker, audit = FakeBroker(), AuditLog(":memory:")
    order = OrderManager(broker, audit).execute(make_intent())
    assert order is not None
    assert len(broker.orders) == 1
    assert not audit.pending_orders()           # journaled past pending_submit


def test_ack_lost_adopts_instead_of_resubmitting():
    broker, audit = FakeBroker(), AuditLog(":memory:")
    broker.ack_lost_next = True
    intent = make_intent()
    order = OrderManager(broker, audit).execute(intent)
    assert order is not None
    assert broker.submit_calls == 1             # resolved by query, not resubmit
    assert set(broker.orders) == {intent.client_order_id}


def test_crash_before_ack_resubmits_same_client_id():
    broker, audit = FakeBroker(), AuditLog(":memory:")
    cid = new_client_order_id("test", "SPY")
    # Simulate: intent journaled, process died before submit ever reached broker.
    audit.upsert_order(cid, symbol="SPY", side="buy", notional=100.0,
                       status="pending_submit")
    om = OrderManager(broker, audit)
    recovered = om.recover_pending()
    assert len(recovered) == 1
    assert set(broker.orders) == {cid}          # SAME id, no new one minted
    # Recovery is itself idempotent: running it again submits nothing new.
    calls_before = broker.submit_calls
    om.recover_pending()
    assert broker.submit_calls == calls_before
    assert len(broker.orders) == 1
