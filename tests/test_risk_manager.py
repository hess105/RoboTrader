"""Risk manager: sizing math, every pre-trade rejection path, breaker trips
at exact thresholds, halt clearing semantics, and restart-proof peak equity.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd
import pytest

from core.models import Position, Side, Signal
from journal.audit import AuditLog
from risk.manager import HaltState, RiskManager

MON, TUE, WED = pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-06"), pd.Timestamp("2026-01-07")
NEXT_MON = pd.Timestamp("2026-01-12")


def make_cfg(**risk_overrides) -> dict:
    risk = {
        "risk_per_trade_pct": 1.0,
        "max_position_notional_pct": 40,
        "max_gross_exposure_pct": 100,
        "max_concurrent_positions": 3,
        "max_per_bucket": 1,
        "daily_loss_halt_pct": 2.5,
        "weekly_loss_halt_pct": 5.0,
        "max_drawdown_halt_pct": 10.0,
        "settled_cash_only": True,
        "day_trade_guard": True,
        "max_spread_pct": 0.05,
    }
    risk.update(risk_overrides)
    return {
        "risk": risk,
        "universe": {"buckets": {"index_equity": ["SPY", "QQQ"], "megacap": ["AAPL"]}},
    }


class FakePortfolio:
    def __init__(self, equity=500.0, settled=500.0, gross=0.0):
        self.equity = equity
        self.settled_cash = settled
        self.gross_exposure = gross
        self.positions: dict[str, Position] = {}
        self.bought_today: set[str] = set()
        self.pending_entries: set[str] = set()


def sig(sym="SPY", side=Side.BUY, stop=97.6, reason="test") -> Signal:
    return Signal("test", sym, side, reason,
                  Decimal(str(stop)) if stop is not None else None, MON)


def pos(sym, bucket) -> Position:
    return Position(sym, Decimal("1"), Decimal("100"), None,
                    datetime(2026, 1, 2, tzinfo=timezone.utc), bucket)


def make_rm(**risk_overrides) -> RiskManager:
    return RiskManager(make_cfg(**risk_overrides), AuditLog(":memory:"))


# --- sizing ---

def test_sizing_risk_fraction_then_notional_cap():
    rm, pf = make_rm(), FakePortfolio()
    # risk $5, stop 2.4% away -> raw notional 208.33, capped at 40% of 500
    intent = rm.approve(sig(stop=97.6), pf, price=100.0)
    assert float(intent.notional) == pytest.approx(200.0)


def test_sizing_uncapped_when_stop_wider():
    intent = make_rm().approve(sig(stop=95.0), FakePortfolio(), price=100.0)
    assert float(intent.notional) == pytest.approx(100.0)  # 5 * 100 / 5


def test_sizing_capped_by_settled_cash():
    intent = make_rm().approve(sig(stop=95.0), FakePortfolio(settled=50.0), price=100.0)
    assert float(intent.notional) == pytest.approx(50.0)


# --- rejection paths ---

def test_rejects_below_min_notional():
    assert make_rm().approve(sig(stop=95.0), FakePortfolio(settled=0.5), price=100.0) is None


def test_rejects_entry_without_stop():
    assert make_rm().approve(sig(stop=None), FakePortfolio(), price=100.0) is None


def test_rejects_stop_above_price():
    assert make_rm().approve(sig(stop=101.0), FakePortfolio(), price=100.0) is None


def test_rejects_at_max_concurrent_positions():
    rm, pf = make_rm(max_per_bucket=5), FakePortfolio()
    pf.positions = {s: pos(s, "other") for s in ("A", "B", "C")}
    assert rm.approve(sig(), pf, price=100.0) is None


def test_pending_entries_count_toward_position_limit():
    rm, pf = make_rm(max_per_bucket=5), FakePortfolio()
    pf.positions = {s: pos(s, "other") for s in ("A", "B")}
    pf.pending_entries = {"C"}
    assert rm.approve(sig(), pf, price=100.0) is None


def test_rejects_second_position_in_same_bucket():
    rm, pf = make_rm(), FakePortfolio()
    pf.positions = {"SPY": pos("SPY", "index_equity")}
    assert rm.approve(sig("QQQ"), pf, price=100.0) is None
    assert rm.approve(sig("AAPL"), pf, price=100.0) is not None


def test_per_bucket_override_tightens_single_name_buckets():
    rm, pf = make_rm(max_per_bucket=2,
                     max_per_bucket_overrides={"megacap": 1}), FakePortfolio()
    rm.buckets["MSFT"] = "megacap"
    rm.buckets["AAPL"] = "megacap"
    pf.positions = {"SPY": pos("SPY", "index_equity"), "AAPL": pos("AAPL", "megacap")}
    assert rm.approve(sig("QQQ"), pf, price=100.0) is not None   # ETF bucket: cap 2
    assert rm.approve(sig("MSFT"), pf, price=100.0) is None      # override: cap 1


def test_rejects_wide_spread():
    assert make_rm().approve(sig(), FakePortfolio(), price=100.0, spread_pct=0.2) is None


def test_day_trade_guard_defers_same_day_exit():
    rm, pf = make_rm(), FakePortfolio()
    pf.positions = {"SPY": pos("SPY", "index_equity")}
    pf.bought_today = {"SPY"}
    assert rm.approve(sig("SPY", Side.SELL, stop=None), pf, price=100.0) is None
    pf.bought_today.clear()
    exit_intent = rm.approve(sig("SPY", Side.SELL, stop=None), pf, price=100.0)
    assert exit_intent is not None and exit_intent.qty == Decimal("1")


# --- breakers ---

def test_daily_loss_halts_entries_but_not_exits_and_clears_next_session():
    rm, pf = make_rm(), FakePortfolio()
    pf.positions = {"SPY": pos("SPY", "index_equity")}
    rm.new_session(MON)
    rm.on_mark(MON, 500.0)
    rm.on_mark(MON, 487.0)                      # -2.6% day
    assert rm.halt == HaltState.DAILY_LOSS
    assert rm.approve(sig("AAPL"), pf, price=100.0) is None
    assert rm.approve(sig("SPY", Side.SELL, stop=None), pf, price=100.0) is not None
    rm.new_session(TUE)
    assert rm.halt == HaltState.NONE


def test_weekly_loss_outranks_daily_and_clears_next_week():
    rm = make_rm()
    rm.new_session(MON)
    rm.on_mark(MON, 500.0)
    rm.on_mark(MON, 490.0)                      # -2.0% day, week baseline 500
    rm.new_session(TUE)
    rm.on_mark(TUE, 490.0)
    rm.on_mark(TUE, 474.0)                      # day -3.3%, week -5.2%
    assert rm.halt == HaltState.WEEKLY_LOSS
    rm.new_session(WED)                          # same week: stays halted
    assert rm.halt == HaltState.WEEKLY_LOSS
    rm.new_session(NEXT_MON)
    assert rm.halt == HaltState.NONE


def test_drawdown_halts_everything_until_manual_reset():
    rm, pf = make_rm(), FakePortfolio()
    pf.positions = {"SPY": pos("SPY", "index_equity")}
    rm.new_session(MON)
    rm.on_mark(MON, 500.0)
    rm.on_mark(MON, 449.0)                      # -10.2% from peak
    assert rm.halt == HaltState.DRAWDOWN
    assert rm.approve(sig("SPY", Side.SELL, stop=None), pf, price=90.0) is None
    protective = sig("SPY", Side.SELL, stop=None, reason="protective stop")
    assert rm.approve(protective, pf, price=90.0) is not None
    rm.new_session(NEXT_MON)                     # time does NOT clear a drawdown halt
    assert rm.halt == HaltState.DRAWDOWN
    with pytest.raises(ValueError):
        rm.manual_reset("  ")
    rm.manual_reset("post-mortem written: sized down, resuming")
    assert rm.halt == HaltState.NONE


def test_drawdown_reset_rebases_peak_and_survives_restart(tmp_path):
    db = str(tmp_path / "audit.sqlite")
    rm = RiskManager(make_cfg(), AuditLog(db))
    rm.new_session(MON)
    rm.on_mark(MON, 500.0)
    rm.on_mark(MON, 449.0)
    assert rm.halt == HaltState.DRAWDOWN
    rm.manual_reset("post-mortem written")
    assert rm.peak_equity == 449.0              # rebased, else next mark re-trips
    rm.new_session(TUE)
    rm.on_mark(TUE, 445.0)
    assert rm.halt == HaltState.WEEKLY_LOSS     # week's -11% still counts: stays
    rm.new_session(NEXT_MON)                    # conservative until the new week
    rm.on_mark(NEXT_MON, 445.0)                 # -0.9% from rebased peak: clean
    assert rm.halt == HaltState.NONE
    rm2 = RiskManager(make_cfg(), AuditLog(db))  # restart honors the rebase
    assert rm2.peak_equity == 449.0


def test_peak_equity_survives_restart(tmp_path):
    db = str(tmp_path / "audit.sqlite")
    rm1 = RiskManager(make_cfg(), AuditLog(db))
    rm1.new_session(MON)
    rm1.on_mark(MON, 500.0)
    rm1.on_mark(MON, 520.0)
    rm2 = RiskManager(make_cfg(), AuditLog(db))   # simulated restart
    assert rm2.peak_equity == 520.0


def test_vol_targeting_derisks_loud_markets_only():
    rm = make_rm(vol_target_pct=15.0, daily_loss_halt_pct=90,
                 weekly_loss_halt_pct=90, max_drawdown_halt_pct=90)
    days = pd.bdate_range("2026-01-05", periods=25)
    calm = [500 * (1.0002 ** i) for i in range(25)]          # ~0.3% annualized vol
    for d, eq in zip(days, calm):
        rm.new_session(d)
        rm.on_mark(d, eq)
    assert rm.vol_scale() == 1.0                             # never sizes UP
    rm2 = make_rm(vol_target_pct=15.0, daily_loss_halt_pct=90,
                  weekly_loss_halt_pct=90, max_drawdown_halt_pct=90)
    loud = [500 * (1 + (0.03 if i % 2 else -0.03)) ** i for i in range(25)]
    for d, eq in zip(days, loud):                            # ~48% annualized vol
        rm2.new_session(d)
        rm2.on_mark(d, eq)
    scale = rm2.vol_scale()
    assert 0.25 <= scale < 0.5                               # sharply de-risked
    intent = rm2.approve(sig(stop=95.0), FakePortfolio(equity=500, settled=500), price=100.0)
    assert float(intent.notional) == pytest.approx(100.0 * scale, rel=1e-6)


def test_vol_window_survives_restart(tmp_path):
    db = str(tmp_path / "audit.sqlite")
    rm = RiskManager(make_cfg(vol_target_pct=15.0), AuditLog(db))
    days = pd.bdate_range("2026-01-05", periods=15)
    for i, d in enumerate(days):
        rm.on_mark(d, 500 * (1 + (0.03 if i % 2 else -0.03)) ** i)
    rm2 = RiskManager(make_cfg(vol_target_pct=15.0), AuditLog(db))
    assert rm2.vol_scale() == pytest.approx(rm.vol_scale(), rel=1e-6)


def test_warning_journaled_at_80pct_of_limit():
    rm = make_rm()
    audit = rm.audit
    rm.new_session(MON)
    rm.on_mark(MON, 500.0)
    rm.on_mark(MON, 489.5)                      # -2.1% = 84% of daily limit
    assert rm.halt == HaltState.NONE
    assert audit.query("risk_warning")
