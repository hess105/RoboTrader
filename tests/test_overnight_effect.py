"""Overnight-effect signal logic: intraday-reversal ranking and the
strategy's structural filters (price, liquidity, volatility floor).
"""
from __future__ import annotations

import pandas as pd

from core.models import Side
from data.view import HistoryView
from strategies.overnight_effect import OvernightEffect

PARAMS = {
    "atr_period": 5, "stop_atr_mult": 2.5, "min_atr_pct": 0.0,
    "min_price": 10, "min_avg_dollar_volume": 0, "dollar_volume_lookback": 5,
    "top_n": 2,
}


def _series(n: int, base: float, day_open, day_close, volume=5_000_000) -> pd.DataFrame:
    """n warmup days of flat `base` bars, then one final day with the given
    open/close (today's bar the strategy scores)."""
    dates = pd.bdate_range("2024-01-02", periods=n + 1)
    opens = [base] * n + [day_open]
    closes = [base] * n + [day_close]
    return pd.DataFrame({
        "open": opens, "high": [max(o, c) + 1 for o, c in zip(opens, closes)],
        "low": [min(o, c) - 1 for o, c in zip(opens, closes)],
        "close": closes, "volume": [volume] * (n + 1),
    }, index=dates)


def make_view(frames: dict[str, pd.DataFrame]) -> HistoryView:
    asof = next(iter(frames.values())).index[-1]
    return HistoryView(frames, asof=asof)


def test_ranks_biggest_intraday_washout_first():
    frames = {
        "BIG": _series(10, 100.0, day_open=100.0, day_close=95.0),   # -5%
        "SMALL": _series(10, 100.0, day_open=100.0, day_close=98.0),  # -2%
        "FLAT": _series(10, 100.0, day_open=100.0, day_close=100.0),  # 0%
    }
    view = make_view(frames)
    signals = OvernightEffect(PARAMS).on_daily_close(view, {})
    symbols = [s.symbol for s in signals]
    assert symbols[0] == "BIG"
    assert "FLAT" not in symbols                # score <= 0 never buys
    assert all(s.side is Side.BUY for s in signals)


def test_top_n_caps_the_nightly_basket():
    frames = {
        f"S{i}": _series(10, 100.0, day_open=100.0, day_close=100.0 - i)
        for i in range(1, 6)                      # 5 candidates, all selling off
    }
    view = make_view(frames)
    signals = OvernightEffect(PARAMS).on_daily_close(view, {})
    assert len(signals) == int(PARAMS["top_n"])


def test_filters_below_min_price():
    frames = {"CHEAP": _series(10, 5.0, day_open=5.0, day_close=4.5)}   # -10%, but < min_price
    view = make_view(frames)
    signals = OvernightEffect(PARAMS).on_daily_close(view, {})
    assert signals == []


def test_filters_below_liquidity_floor():
    p = {**PARAMS, "min_avg_dollar_volume": 1_000_000_000}
    frames = {"THIN": _series(10, 100.0, day_open=100.0, day_close=95.0, volume=1000)}
    view = make_view(frames)
    signals = OvernightEffect(p).on_daily_close(view, {})
    assert signals == []


def test_stop_below_entry_close():
    frames = {"BIG": _series(10, 100.0, day_open=100.0, day_close=95.0)}
    view = make_view(frames)
    signals = OvernightEffect(PARAMS).on_daily_close(view, {})
    assert len(signals) == 1
    assert float(signals[0].stop_price) < 95.0


def test_already_held_symbol_not_rebought():
    frames = {"BIG": _series(10, 100.0, day_open=100.0, day_close=95.0)}
    view = make_view(frames)
    signals = OvernightEffect(PARAMS).on_daily_close(view, {"BIG": object()})
    assert signals == []
