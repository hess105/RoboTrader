"""Momentum rotation signal logic and composite strategy routing."""
from __future__ import annotations

from decimal import Decimal

import pandas as pd

from core.models import Position, Side, Signal
from data.view import HistoryView
from strategies import build_strategy
from strategies.base import Strategy
from strategies.composite import CompositeStrategy
from strategies.momentum_rotation import MomentumRotation

PARAMS = {"symbols": None, "top_n": 1, "score_fast": 20, "score_slow": 40,
          "min_score": 0.0, "exit_rank": 2, "stop_atr_mult": 3.0, "atr_period": 5}

# Monday 2024-03-04 follows Friday 2024-03-01: an ISO-week boundary.
DATES = pd.bdate_range("2023-12-01", "2024-03-04")


def frame(slope: float) -> pd.DataFrame:
    close = pd.Series([100.0 + slope * i for i in range(len(DATES))], index=DATES)
    return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1,
                         "close": close, "volume": 1})


def make_view(slopes: dict[str, float], asof=DATES[-1]) -> HistoryView:
    return HistoryView({s: frame(k) for s, k in slopes.items()}, asof=asof)


def pos(sym: str, strategy: str) -> Position:
    return Position(sym, Decimal("1"), Decimal("100"), Decimal("90"),
                    DATES[-10], "test", strategy=strategy)


def test_buys_strongest_positive_momentum_on_week_start():
    view = make_view({"UP": 1.0, "FLAT": 0.0, "DOWN": -0.5})
    signals = MomentumRotation(PARAMS).on_daily_close(view, {})
    assert len(signals) == 1
    sig = signals[0]
    assert sig.symbol == "UP" and sig.side is Side.BUY
    assert sig.limit_price is None                      # momentum chases, no limit
    assert float(sig.stop_price) < float(frame(1.0)["close"].iloc[-1])


def test_no_signals_midweek():
    view = make_view({"UP": 1.0}, asof=DATES[-2])       # Friday preceded by Thursday
    assert MomentumRotation(PARAMS).on_daily_close(view, {}) == []


def test_absolute_filter_stays_out_when_everything_falls():
    view = make_view({"A": -0.5, "B": -1.0})
    assert MomentumRotation(PARAMS).on_daily_close(view, {}) == []


def test_exits_on_rank_drop_and_negative_score():
    view = make_view({"A": 1.0, "B": 0.8, "C": 0.6, "LOSER": -0.5})
    held = {"C": pos("C", "momentum_rotation"), "LOSER": pos("LOSER", "momentum_rotation")}
    signals = MomentumRotation(PARAMS).on_daily_close(view, held)
    sells = {s.symbol: s.reason for s in signals if s.side is Side.SELL}
    assert "LOSER" in sells and "score" in sells["LOSER"]
    assert "C" in sells and "rank" in sells["C"]        # rank 3 > exit_rank 2


class StubSleeve(Strategy):
    def __init__(self, name: str, emits: list[Signal]):
        super().__init__({})
        self.name, self.emits, self.saw = name, emits, None

    def on_daily_close(self, view, positions):
        self.saw = dict(positions)
        return list(self.emits)


def buy(strategy: str, sym: str) -> Signal:
    return Signal(strategy, sym, Side.BUY, "t", Decimal("90"), DATES[-1])


def test_composite_routes_positions_by_owner_and_dedupes_buys():
    a = StubSleeve("a", [buy("a", "SPY"), buy("a", "QQQ")])
    b = StubSleeve("b", [buy("b", "SPY"), buy("b", "GLD")])
    comp = CompositeStrategy([a, b])
    positions = {"XLE": pos("XLE", "a"), "TLT": pos("TLT", "b"),
                 "IWM": pos("IWM", "")}                  # unknown owner -> first child
    signals = comp.on_daily_close(None, positions)
    assert set(a.saw) == {"XLE", "IWM"}
    assert set(b.saw) == {"TLT"}
    buys = [s for s in signals if s.side is Side.BUY]
    assert [(s.strategy, s.symbol) for s in buys] == [("a", "SPY"), ("a", "QQQ"),
                                                      ("b", "GLD")]   # b's SPY deduped


def test_factory_builds_composite_and_resolves_buckets():
    cfg = {
        "universe": {"buckets": {"x": ["SPY", "QQQ"], "y": ["GLD"]}},
        "strategies": [
            {"name": "trend_pullback", "params": {"trend_sma": 200, "pullback_rsi_period": 2,
             "pullback_rsi_max": 10, "exit_rsi_min": 70, "max_hold_days": 5,
             "atr_period": 14, "stop_atr_mult": 2.0}},
            {"name": "momentum_rotation", "params": {**PARAMS, "buckets": ["x", "y"]}},
        ],
    }
    strat = build_strategy(cfg)
    assert isinstance(strat, CompositeStrategy)
    momo = strat.children[1]
    assert momo.params["symbols"] == ["SPY", "QQQ", "GLD"]
