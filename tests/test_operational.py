"""Operational modules: kill switch flattens and halts, reconciler catches
journal/broker divergence, tax ledger does FIFO + wash flags correctly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from core.models import Order, OrderStatus, Position, Side
from execution.reconciler import Reconciler
from journal.audit import AuditLog
from journal.tax import TaxLedger
from risk.kill_switch import KillSwitch
from risk.manager import HaltState, RiskManager
from tests.test_risk_manager import make_cfg


def pos(sym: str, qty: str) -> Position:
    return Position(sym, Decimal(qty), Decimal("100"), None,
                    datetime(2026, 1, 5, tzinfo=timezone.utc), "test")


class FakeBroker:
    def __init__(self, positions=(), orders=()):
        self._positions = list(positions)
        self._orders = list(orders)
        self.cancelled: list[str] = []
        self.flattened = False

    def positions(self):
        return list(self._positions)

    def open_orders(self):
        return list(self._orders)

    def cancel(self, cid):
        self.cancelled.append(cid)
        self._orders = [o for o in self._orders if o.client_order_id != cid]

    def close_all_positions(self):
        self.flattened = True
        self._positions = []
        return []


def open_order(cid: str) -> Order:
    return Order(client_order_id=cid, broker_order_id="b1", symbol="SPY",
                 side=Side.BUY, status=OrderStatus.SUBMITTED,
                 notional=Decimal("100"), qty=None)


# --- kill switch ---

def test_kill_switch_cancels_flattens_and_halts():
    broker = FakeBroker(positions=[pos("SPY", "1.5")], orders=[open_order("rt-x-1")])
    audit = AuditLog(":memory:")
    risk = RiskManager(make_cfg(), audit)
    flat = KillSwitch(broker, risk, audit).fire(
        "test", "unit", poll_timeout_sec=1, poll_interval_sec=0)
    assert flat is True
    assert broker.cancelled == ["rt-x-1"]
    assert broker.flattened
    assert risk.halt == HaltState.KILL_SWITCH
    assert audit.query("kill_switch") and audit.query("kill_switch_done")


def test_kill_switch_reports_not_flat():
    class StubbornBroker(FakeBroker):
        def close_all_positions(self):        # broker error: nothing closes
            return []

    broker = StubbornBroker(positions=[pos("SPY", "1")])
    audit = AuditLog(":memory:")
    risk = RiskManager(make_cfg(), audit)
    flat = KillSwitch(broker, risk, audit).fire(
        "test", "unit", poll_timeout_sec=0.1, poll_interval_sec=0.05)
    assert flat is False
    assert risk.halt == HaltState.KILL_SWITCH   # halted regardless
    assert audit.query("kill_switch_error")


# --- reconciler ---

def test_reconcile_clean_when_journal_matches_broker():
    audit = AuditLog(":memory:")
    audit.event("fill", symbol="SPY", side="buy", qty=1.5, price=100.0)
    report = Reconciler(FakeBroker(positions=[pos("SPY", "1.5")]), audit).run()
    assert report.clean


def test_reconcile_flags_divergence_both_ways():
    audit = AuditLog(":memory:")
    audit.event("fill", symbol="SPY", side="buy", qty=2.0, price=100.0)   # journal: 2
    broker = FakeBroker(positions=[pos("SPY", "1.5"), pos("QQQ", "1")])   # broker: 1.5 + surprise QQQ
    report = Reconciler(broker, audit).run()
    assert not report.clean
    assert len(report.mismatches) == 2


# --- tax ledger ---

def _fill(audit, side, qty, price, when):
    audit.event("fill", symbol="SPY", side=side, qty=qty, price=price, detail=when)


def test_tax_fifo_and_terms():
    audit = AuditLog(":memory:")
    _fill(audit, "buy", 10, 100.0, "2024-01-10T15:00:00+00:00")
    _fill(audit, "buy", 5, 110.0, "2024-06-01T15:00:00+00:00")
    _fill(audit, "sell", 12, 120.0, "2025-03-01T15:00:00+00:00")
    rows = TaxLedger(audit).realized(2025)
    assert len(rows) == 2                       # 10 from lot 1, 2 from lot 2
    assert rows[0].qty == 10 and rows[0].gain == pytest.approx(200.0)
    assert rows[0].long_term                    # held > 1 year
    assert rows[1].qty == 2 and rows[1].gain == pytest.approx(20.0)
    assert not rows[1].long_term


def test_tax_wash_sale_flag(tmp_path):
    audit = AuditLog(":memory:")
    _fill(audit, "buy", 3, 110.0, "2024-01-10T15:00:00+00:00")
    _fill(audit, "sell", 3, 90.0, "2024-04-01T15:00:00+00:00")    # loss
    _fill(audit, "buy", 1, 95.0, "2024-04-15T15:00:00+00:00")     # re-buy in 30d
    ledger = TaxLedger(audit)
    rows = ledger.realized(2024)
    assert rows[0].gain < 0 and rows[0].wash
    out = tmp_path / "8949.csv"
    assert ledger.export_8949_csv(2024, str(out)) == 1
    assert "W" in out.read_text()
