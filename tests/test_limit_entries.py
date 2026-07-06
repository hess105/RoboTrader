"""Resting-limit entry mechanics (entry_limit_atr) and the strategy's
structural filters (volatility floor, market regime gate)."""
from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from core.models import Side, Signal
from data.view import HistoryView
from journal.audit import AuditLog
from risk.manager import RiskManager
from strategies.base import Strategy
from strategies.trend_pullback import TrendPullback
from tests.test_backtest_no_lookahead import COST, DATES, make_bars, make_settings
from tests.test_risk_manager import FakePortfolio, make_cfg
from tests.test_trend_pullback import PARAMS, make_view, uptrend_with_washout
from backtest.engine import BacktestEngine


class LimitProbe(Strategy):
    name = "limit_probe"

    def __init__(self, buy_on: pd.Timestamp, limit: float, stop: float = 10.0):
        super().__init__({})
        self.buy_on, self.limit, self.stop = buy_on, limit, stop

    def warmup_bars(self) -> int:
        return 10

    def on_daily_close(self, view, positions):
        if view.asof == self.buy_on and "AAA" not in positions:
            return [Signal(self.name, "AAA", Side.BUY, "probe",
                           Decimal(str(self.stop)), view.asof,
                           limit_price=Decimal(str(self.limit)))]
        return []


def bars_with_low(day: int, low_val: float) -> pd.DataFrame:
    """make_bars, but the dip value is configurable (and only on `day`)."""
    df = make_bars().droplevel(0).copy()
    df.iloc[day, df.columns.get_loc("low")] = low_val
    return pd.concat({"AAA": df}, names=["symbol", "ts"])


def run(strategy, bars):
    settings = make_settings(["AAA"])
    risk = RiskManager(settings.raw, AuditLog(":memory:"))
    engine = BacktestEngine(settings, strategy, risk)
    return engine.run(bars), risk


def test_limit_fills_exactly_at_limit_when_touched():
    # DAY limit lives only on the fill day (T+1 = DATES[11]): open there is
    # ~111; dip the low to 105 so a 108 limit is touched but not marketable.
    bars = bars_with_low(11, 105.0)
    result, _ = run(LimitProbe(DATES[10], limit=108.0), bars)
    # position opened at the limit price exactly (passive fill, no added cost)
    assert result.open_positions and result.open_positions[0]["avg_entry"] == pytest.approx(108.0)


def test_limit_never_touched_lapses_and_refunds():
    bars = make_bars()                             # lows never reach 50
    result, risk = run(LimitProbe(DATES[10], 50.0), bars)
    assert result.trades == [] and result.open_positions == []
    assert risk.audit.query("order_expired")
    # reservation refunded: equity intact
    assert result.equity.iloc[-1] == pytest.approx(10_000)


def test_marketable_limit_fills_at_open_with_costs_capped_at_limit():
    bars = make_bars()
    result, _ = run(LimitProbe(DATES[10], limit=500.0), bars)   # far above open
    expected = (100.0 + 11) * (1 + COST)           # open + adverse costs, < limit
    assert result.open_positions[0]["avg_entry"] == pytest.approx(expected)


def test_risk_sizes_against_limit_price():
    rm = RiskManager(make_cfg(), AuditLog(":memory:"))
    sig = Signal("t", "SPY", Side.BUY, "t", Decimal("93"), pd.Timestamp("2026-01-05"),
                 limit_price=Decimal("98"))
    intent = rm.approve(sig, FakePortfolio(), price=100.0)
    # risk $5 over a $5 stop distance FROM THE LIMIT (98-93) -> $98 notional
    assert float(intent.notional) == pytest.approx(98.0)
    assert intent.limit_price == Decimal("98")


def test_volatility_floor_blocks_quiet_symbols():
    view = make_view(uptrend_with_washout())
    quiet = TrendPullback({**PARAMS, "min_atr_pct": 50.0})   # absurd floor
    assert quiet.on_daily_close(view, {}) == []
    normal = TrendPullback({**PARAMS, "min_atr_pct": 0.5})
    assert len(normal.on_daily_close(view, {})) == 1


def test_market_filter_gates_entries_not_exits():
    # AAA washed out in an uptrend, but market proxy SPY is in a downtrend.
    dates = pd.bdate_range("2024-01-02", periods=63)
    aaa = make_view(uptrend_with_washout()).history("AAA")
    spy_close = pd.Series([200.0 - i for i in range(63)], index=dates)
    spy = pd.DataFrame({"open": spy_close, "high": spy_close + 1,
                        "low": spy_close - 1, "close": spy_close, "volume": 1})
    view = HistoryView({"AAA": aaa, "SPY": spy}, asof=dates[-1])
    strat = TrendPullback({**PARAMS, "market_filter": True})
    assert strat.on_daily_close(view, {}) == []             # entry gated
    from core.models import Position
    positions = {"AAA": Position("AAA", Decimal("1"), Decimal("155"),
                                 Decimal("140"), dates[-7], "test")}
    exits = strat.on_daily_close(view, positions)           # exits still flow
    assert exits and all(s.side is Side.SELL for s in exits)


def test_limit_entry_signal_carries_limit_and_derived_stop():
    strat = TrendPullback({**PARAMS, "entry_limit_atr": 0.25})
    signals = strat.on_daily_close(make_view(uptrend_with_washout()), {})
    assert len(signals) == 1
    sig = signals[0]
    assert sig.limit_price is not None and float(sig.limit_price) < 153.0
    assert float(sig.stop_price) < float(sig.limit_price)
