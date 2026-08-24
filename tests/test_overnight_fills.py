"""Overnight-only fill timing: an overnight_only strategy's BUY fills at its
OWN signal bar's close (not the next open, unlike every other strategy), and
the resulting position is force-closed at the very next open regardless of
halt state.
"""
from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from backtest.engine import BacktestEngine, BTPortfolio
from core.models import Position, Side, Signal
from journal.audit import AuditLog
from risk.manager import HaltState, RiskManager
from strategies.base import Strategy
from tests.test_backtest_no_lookahead import COST, DATES, make_bars, make_settings


class OvernightProbe(Strategy):
    name = "overnight_probe"
    overnight_only = True

    def __init__(self, buy_on: pd.Timestamp, symbol: str, stop_frac: float = 0.5):
        super().__init__({})
        self.buy_on, self.symbol, self.stop_frac = buy_on, symbol, stop_frac

    def warmup_bars(self) -> int:
        return 10

    def on_daily_close(self, view, positions):
        if view.asof == self.buy_on and self.symbol not in positions:
            px = float(view.history(self.symbol)["close"].iloc[-1])
            return [Signal(self.name, self.symbol, Side.BUY, "overnight probe entry",
                           Decimal(str(round(px * self.stop_frac, 2))), view.asof)]
        return []


def run(strategy, bars):
    settings = make_settings(["AAA"])
    risk = RiskManager(settings.raw, AuditLog(":memory:"))
    return BacktestEngine(settings, strategy, risk).run(bars)


def test_overnight_entry_fills_at_own_close_not_next_open():
    strat = OvernightProbe(buy_on=DATES[10], symbol="AAA")
    result = run(strat, make_bars())
    assert len(result.trades) == 1
    tr = result.trades[0]
    assert tr.entry_ts == DATES[10]                              # not DATES[11]
    assert tr.entry_price == pytest.approx((100.0 + 10 + 0.2) * (1 + COST))
    assert tr.exit_ts == DATES[11]
    assert tr.exit_price == pytest.approx((100.0 + 11) * (1 - COST))
    assert tr.exit_reason == "overnight_exit (next open)"


def test_overnight_position_never_carries_past_one_session():
    # Even though the probe would happily re-enter, the position is always
    # gone by the next session's open — no multi-day overnight hold is
    # structurally reachable.
    strat = OvernightProbe(buy_on=DATES[10], symbol="AAA")
    result = run(strat, make_bars())
    assert result.open_positions == []


def test_overnight_force_exit_ignores_active_halt():
    settings = make_settings(["AAA"])
    risk = RiskManager(settings.raw, AuditLog(":memory:"))
    strat = OvernightProbe(buy_on=DATES[10], symbol="AAA")
    engine = BacktestEngine(settings, strat, risk)

    pf = BTPortfolio(10_000)
    pf.positions["AAA"] = Position(
        symbol="AAA", qty=Decimal("10"), avg_entry=Decimal("100"),
        stop_price=None, opened_at=DATES[0], bucket="test", strategy=strat.name,
    )
    risk.engage(HaltState.DRAWDOWN, "test-injected halt")
    today = {"AAA": pd.Series({"open": 105.0, "high": 106.0, "low": 104.0,
                               "close": 105.5, "volume": 1_000_000})}
    trades: list = []

    engine._force_close_overnight(pf, today, DATES[11], trades)

    assert "AAA" not in pf.positions
    assert len(trades) == 1
    assert trades[0].exit_reason == "overnight_exit (next open)"
    assert risk.halt == HaltState.DRAWDOWN        # halt untouched, and didn't block the exit
