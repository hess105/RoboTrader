"""Scaffold smoke tests: every module imports, config loads and defaults to
paper, and the two already-implemented pieces (indicators, cost model) are
numerically sane. Keeps `make test` meaningful from day one — `make live`
refuses to start unless this suite passes.
"""
from __future__ import annotations

import importlib
from decimal import Decimal

import pandas as pd
import pytest

MODULES = [
    "core.models",
    "core.settings",
    "data.provider",
    "data.view",
    "data.cache",
    "data.alpaca_data",
    "strategies.base",
    "strategies.indicators",
    "strategies.trend_pullback",
    "backtest.engine",
    "backtest.costs",
    "backtest.metrics",
    "execution.broker",
    "execution.alpaca_client",
    "execution.order_manager",
    "execution.reconciler",
    "risk.manager",
    "risk.kill_switch",
    "journal.audit",
    "journal.tax",
    "monitoring.alerts",
    "monitoring.health",
    "service.engine",
    "service.api",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module):
    importlib.import_module(module)


def test_default_mode_is_paper():
    from core.models import Mode
    from core.settings import load_settings

    assert load_settings().mode is Mode.PAPER
    assert load_settings("config/paper.yaml").mode is Mode.PAPER


def test_base_config_has_required_risk_keys():
    from core.settings import load_settings

    risk = load_settings().raw["risk"]
    for key in ("risk_per_trade_pct", "daily_loss_halt_pct",
                "max_drawdown_halt_pct", "max_concurrent_positions",
                "settled_cash_only", "day_trade_guard"):
        assert key in risk


def test_cost_model_is_adverse_both_directions():
    from backtest.costs import CostModel
    from core.models import Side

    cm = CostModel()
    open_price = Decimal("100")
    assert cm.fill_price(open_price, Side.BUY) > open_price
    assert cm.fill_price(open_price, Side.SELL) < open_price


def test_alpaca_regulatory_sell_fees():
    from backtest.costs import CostModel

    cm = CostModel()
    # Typical small-account sell: 2 shares, $200 proceeds.
    # SEC: 200 * 27.80/1M = $0.00556 -> $0.01; TAF: 2 * 0.000166 -> $0.01.
    assert cm.sell_fees(qty=2, proceeds=200.0) == pytest.approx(0.02)
    # Large sale: SEC scales with proceeds, TAF capped at $8.30.
    big = cm.sell_fees(qty=100_000, proceeds=1_000_000.0)
    assert big == pytest.approx(27.80 + 8.30)
    # Buys carry no fees (engine never calls this on buys, but be explicit).
    assert cm.sell_fees(qty=0, proceeds=0.0) == 0.0


def test_rsi_bounds_and_extremes():
    from strategies.indicators import rsi

    up = pd.Series(range(1, 40), dtype=float)
    down = pd.Series(range(40, 1, -1), dtype=float)
    assert rsi(up, 2).iloc[-1] > 90
    assert rsi(down, 2).iloc[-1] < 10


def test_atr_positive_and_sma_matches_mean():
    from strategies.indicators import atr, sma

    close = pd.Series([100, 102, 101, 103, 104, 102, 105], dtype=float)
    high, low = close + 1, close - 1
    assert (atr(high, low, close, 3).dropna() > 0).all()
    assert sma(close, 3).iloc[-1] == pytest.approx(close.iloc[-3:].mean())


def test_client_order_id_unique_per_call():
    from core.models import new_client_order_id

    a = new_client_order_id("trend_pullback", "SPY")
    b = new_client_order_id("trend_pullback", "SPY")
    assert a != b and a.startswith("rt-trend_pullback-SPY-")
