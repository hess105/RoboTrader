"""SimBroker bookkeeping: the multi-day simulator tracks its own cash/
position book (seeded once from a real account) instead of re-reading the
broker each day, so these prove buys/sells/averaging/equity math are correct
in isolation — the full run_simulated_day() needs live Alpaca credentials
and historical data, so it isn't exercised here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from core.models import Mode, OrderIntent, Position, Side, Signal, new_client_order_id
from service.simulate_day import SimBroker


class FakeRealBroker:
    def __init__(self, equity=1000.0, cash=1000.0, positions=None):
        self._equity = equity
        self._cash = cash
        self._positions = positions or []

    def account_equity(self):
        return Decimal(str(self._equity)), Decimal(str(self._cash)), Decimal(str(self._cash))

    def positions(self):
        return self._positions


def _intent(symbol, side, qty=None, notional=None):
    return OrderIntent(
        client_order_id=new_client_order_id("test", symbol),
        signal=Signal("test", symbol, side, "test", None, datetime.now(timezone.utc)),
        notional=Decimal(str(notional)) if notional is not None else None,
        qty=Decimal(str(qty)) if qty is not None else None,
        limit_price=None,
    )


def test_seed_from_real_starts_the_book():
    real = FakeRealBroker(equity=1000.0, cash=1000.0)
    sb = SimBroker(real, data=None)
    sb.seed_from_real()
    equity, cash, _bp = sb.account_equity()
    assert float(equity) == 1000.0
    assert float(cash) == 1000.0
    assert sb.positions() == []


def test_buy_then_fill_updates_cash_and_position():
    sb = SimBroker(FakeRealBroker(), data=None)
    sb.seed_from_real()
    intent = _intent("AAA", Side.BUY, notional=100.0)
    order = sb.submit(intent)
    assert order.status.value == "submitted"

    sb.set_fill_price("AAA", 10.0)
    filled = sb.get_order_by_client_id(intent.client_order_id)
    assert filled.status.value == "filled"
    assert float(filled.filled_qty) == 10.0          # 100 notional / $10

    equity, cash, _bp = sb.account_equity()
    assert float(cash) == 900.0                       # 1000 - 100
    positions = sb.positions()
    assert len(positions) == 1
    assert positions[0].symbol == "AAA"
    assert float(positions[0].qty) == 10.0
    assert float(positions[0].avg_entry) == 10.0


def test_second_buy_averages_entry_price():
    sb = SimBroker(FakeRealBroker(), data=None)
    sb.seed_from_real()

    i1 = _intent("AAA", Side.BUY, qty=10)
    sb.submit(i1)
    sb.set_fill_price("AAA", 10.0)
    sb.get_order_by_client_id(i1.client_order_id)

    i2 = _intent("AAA", Side.BUY, qty=10)
    sb.submit(i2)
    sb.set_fill_price("AAA", 20.0)
    sb.get_order_by_client_id(i2.client_order_id)

    positions = sb.positions()
    assert len(positions) == 1
    assert float(positions[0].qty) == 20.0
    assert float(positions[0].avg_entry) == 15.0      # (10*10 + 10*20) / 20


def test_sell_reduces_position_and_full_exit_removes_it():
    sb = SimBroker(FakeRealBroker(), data=None)
    sb.seed_from_real()

    buy = _intent("AAA", Side.BUY, qty=10)
    sb.submit(buy)
    sb.set_fill_price("AAA", 10.0)
    sb.get_order_by_client_id(buy.client_order_id)

    partial_sell = _intent("AAA", Side.SELL, qty=4)
    sb.submit(partial_sell)
    sb.set_fill_price("AAA", 12.0)
    sb.get_order_by_client_id(partial_sell.client_order_id)
    positions = sb.positions()
    assert len(positions) == 1
    assert float(positions[0].qty) == 6.0

    full_sell = _intent("AAA", Side.SELL, qty=6)
    sb.submit(full_sell)
    sb.set_fill_price("AAA", 13.0)
    sb.get_order_by_client_id(full_sell.client_order_id)
    assert sb.positions() == []

    equity, cash, _bp = sb.account_equity()
    # 1000 - (10*10) + (4*12) + (6*13) = 1000 - 100 + 48 + 78 = 1026
    assert float(cash) == 1026.0
    assert float(equity) == 1026.0


def test_equity_marks_open_positions_to_market():
    sb = SimBroker(FakeRealBroker(), data=None)
    sb.seed_from_real()
    buy = _intent("AAA", Side.BUY, qty=10)
    sb.submit(buy)
    sb.set_fill_price("AAA", 10.0)
    sb.get_order_by_client_id(buy.client_order_id)

    sb.mark("AAA", 15.0)
    equity, cash, _bp = sb.account_equity()
    assert float(cash) == 900.0
    assert float(equity) == 900.0 + 10 * 15.0         # cash + market value


def test_cancel_and_close_all_positions_raise():
    sb = SimBroker(FakeRealBroker(), data=None)
    try:
        sb.cancel("whatever")
        assert False, "expected NotImplementedError"
    except NotImplementedError:
        pass
    try:
        sb.close_all_positions()
        assert False, "expected NotImplementedError"
    except NotImplementedError:
        pass
