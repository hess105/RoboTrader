"""Trend-pullback signal logic on crafted series: entry only on a washout
within an uptrend, ATR stop below price, and the exit ladder."""
from __future__ import annotations

from datetime import timezone
from decimal import Decimal

import pandas as pd

from core.models import Position, Side
from data.view import HistoryView
from strategies.trend_pullback import TrendPullback

PARAMS = {
    "trend_sma": 20, "pullback_rsi_period": 2, "pullback_rsi_max": 10,
    "exit_rsi_min": 70, "max_hold_days": 5, "atr_period": 5,
    "stop_atr_mult": 2.0,
}


def make_view(closes: list[float]) -> HistoryView:
    dates = pd.bdate_range("2024-01-02", periods=len(closes))
    close = pd.Series(closes, index=dates)
    df = pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close + 1, "low": close - 1, "close": close,
        "volume": 1_000_000,
    })
    return HistoryView({"AAA": df}, asof=dates[-1])


def uptrend_with_washout() -> list[float]:
    prices = [100.0 + i for i in range(60)]      # steady uptrend to 159
    prices += [157.0, 155.0, 153.0]              # three straight down closes:
    return prices                                # Wilder RSI(2) decays below 10


def test_entry_on_washout_in_uptrend():
    view = make_view(uptrend_with_washout())
    signals = TrendPullback(PARAMS).on_daily_close(view, {})
    assert len(signals) == 1
    sig = signals[0]
    assert sig.side is Side.BUY and sig.symbol == "AAA"
    assert sig.stop_price is not None and float(sig.stop_price) < 155.0


def test_no_entry_without_washout():
    view = make_view([100.0 + i for i in range(62)])     # uptrend, RSI pinned high
    assert TrendPullback(PARAMS).on_daily_close(view, {}) == []


def test_no_entry_below_trend_filter():
    prices = [200.0 - i for i in range(60)] + [138.0, 136.0]  # downtrend washout
    assert TrendPullback(PARAMS).on_daily_close(make_view(prices), {}) == []


def test_rsi_exit_when_bounce_completes():
    prices = uptrend_with_washout() + [158.0, 161.0]     # sharp bounce: RSI(2) high
    view = make_view(prices)
    dates = view.history("AAA").index
    # opened_at two bars back; RSI now high -> rsi_exit, not time_stop
    positions = {"AAA": Position("AAA", Decimal("1"), Decimal("155"),
                                 Decimal("150"), dates[-3], "test")}
    signals = TrendPullback(PARAMS).on_daily_close(view, positions)
    exits = [s for s in signals if s.side is Side.SELL]
    assert len(exits) == 1 and exits[0].reason.startswith("rsi_exit")


def test_time_stop_after_max_hold():
    prices = uptrend_with_washout() + [154.0, 153.0, 152.5, 152.0, 151.5, 151.0]
    view = make_view(prices)
    dates = view.history("AAA").index
    positions = {"AAA": Position("AAA", Decimal("1"), Decimal("155"),
                                 Decimal("140"), dates[-7], "test")}
    signals = TrendPullback(PARAMS).on_daily_close(view, positions)
    exits = [s for s in signals if s.side is Side.SELL]
    assert len(exits) == 1 and exits[0].reason.startswith("time_stop")
