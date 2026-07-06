"""Look-ahead prevention and fill realism.

The HistoryView makes future bars structurally unreachable; these tests prove
it, and prove the two fill rules that keep backtests honest: signals on day
T's close fill at T+1's OPEN with adverse costs, and protective stops fire
like broker-resting stop orders (gap-aware).
"""
from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from core.models import Mode, Side, Signal
from core.settings import Settings
from data.view import HistoryView
from journal.audit import AuditLog
from risk.manager import RiskManager
from strategies.base import Strategy

DATES = pd.bdate_range("2024-01-02", periods=30)
COST = 7.5 / 1e4          # half-spread 2.5bps + extra slippage 5bps, per side


def make_bars(symbol="AAA", dip_low_on: int | None = None) -> pd.DataFrame:
    opens = [100.0 + i for i in range(len(DATES))]
    closes = [o + 0.2 for o in opens]
    lows = [o - 1 for o in opens]
    if dip_low_on is not None:
        lows[dip_low_on] = 50.0
    df = pd.DataFrame(
        {"open": opens, "high": [c + 1 for c in closes], "low": lows,
         "close": closes, "volume": [1_000_000] * len(DATES)},
        index=DATES,
    )
    return pd.concat({symbol: df}, names=["symbol", "ts"])


def make_settings(symbols) -> Settings:
    return Settings(mode=Mode.BACKTEST, raw={
        "account": {"starting_capital": 10_000},
        "universe": {"buckets": {"test": list(symbols)}},
        "risk": {
            "risk_per_trade_pct": 2.0, "max_position_notional_pct": 40,
            "max_gross_exposure_pct": 100, "max_concurrent_positions": 5,
            "max_per_bucket": 5, "daily_loss_halt_pct": 90,
            "weekly_loss_halt_pct": 90, "max_drawdown_halt_pct": 90,
            "settled_cash_only": True, "day_trade_guard": True,
        },
        "backtest": {"extra_slippage_bps": 5},
    })


class Probe(Strategy):
    """Buys one symbol on a chosen date, exits after 3 bars; records the most
    recent bar timestamp it was ever shown."""
    name = "probe"

    def __init__(self, buy_on: pd.Timestamp, symbol: str, stop_frac: float = 0.5):
        super().__init__({})
        self.buy_on, self.symbol, self.stop_frac = buy_on, symbol, stop_frac
        self.seen: list[tuple[pd.Timestamp, pd.Timestamp]] = []

    def warmup_bars(self) -> int:
        return 10

    def on_daily_close(self, view, positions):
        for s in view.symbols:
            df = view.history(s)
            if not df.empty:
                self.seen.append((view.asof, df.index.max()))
        signals = []
        if view.asof == self.buy_on and self.symbol not in positions:
            px = float(view.history(self.symbol)["close"].iloc[-1])
            signals.append(Signal(self.name, self.symbol, Side.BUY, "probe entry",
                                  Decimal(str(round(px * self.stop_frac, 2))), view.asof))
        for s, pos in positions.items():
            held = int((view.history(s).index > pd.Timestamp(pos.opened_at)).sum())
            if held >= 3:
                signals.append(Signal(self.name, s, Side.SELL, "probe exit", None, view.asof))
        return signals


def run(strategy, bars):
    settings = make_settings(["AAA"])
    risk = RiskManager(settings.raw, AuditLog(":memory:"))
    return BacktestEngine(settings, strategy, risk).run(bars)


def test_history_view_is_clock_gated():
    df = make_bars().droplevel(0)
    view = HistoryView({"AAA": df}, asof=DATES[4], max_bars=3)
    hist = view.history("AAA")
    assert hist.index.max() == DATES[4]
    assert len(hist) == 3


def test_strategy_never_shown_future_bars():
    strat = Probe(buy_on=DATES[10], symbol="AAA")
    run(strat, make_bars())
    assert strat.seen
    assert all(last <= asof for asof, last in strat.seen)


def test_signal_on_day_t_fills_at_t_plus_1_open_with_adverse_costs():
    strat = Probe(buy_on=DATES[10], symbol="AAA")
    result = run(strat, make_bars())
    assert len(result.trades) == 1
    tr = result.trades[0]
    assert tr.entry_ts == DATES[11]                       # never DATES[10]'s close
    assert tr.entry_price == pytest.approx((100.0 + 11) * (1 + COST))
    # exit signal at close of DATES[14] (3 bars held) -> fills DATES[15] open
    assert tr.exit_ts == DATES[15]
    assert tr.exit_price == pytest.approx((100.0 + 15) * (1 - COST))


def test_protective_stop_fires_intraday_like_a_resting_order():
    # Entry fills DATES[11]; stop = 90% of DATES[10] close = ~99.2;
    # DATES[13]'s low dips to 50 -> stop fills AT the stop price (no gap).
    strat = Probe(buy_on=DATES[10], symbol="AAA", stop_frac=0.9)
    result = run(strat, make_bars(dip_low_on=13))
    assert len(result.trades) == 1
    tr = result.trades[0]
    stop = round(110.2 * 0.9, 2)
    assert tr.exit_ts == DATES[13]
    assert tr.exit_reason == "protective stop"
    assert tr.exit_price == pytest.approx(stop * (1 - COST))
