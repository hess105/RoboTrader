"""Slippage / spread / commission / regulatory-fee model for the backtester.

Fill price for a BUY = next_open * (1 + half_spread_pct + extra_bps/1e4);
SELL is the mirror. Half-spread defaults to 2.5bps for the liquid universe —
after paper trading, replace assumptions with paper-measured spreads and
re-run the backtest as part of the paper->live gate.

Alpaca commission is $0 on stocks/ETFs, but SELLS carry regulatory
pass-through fees, each rounded UP to the cent per trade the way brokers
invoice them:
  * SEC Section 31 fee on sale proceeds (rate resets ~annually — verify
    against Alpaca's docs; config: backtest.sec_fee_per_million)
  * FINRA Trading Activity Fee per share sold, capped per trade
    (config: backtest.taf_per_share)
At this account's ~$150-200 position sizes that's ~$0.02/sell (~1bp) —
small, but real money at small size.
"""
from __future__ import annotations

import math
from decimal import Decimal

from core.models import Side


class CostModel:
    def __init__(self, half_spread_bps: Decimal = Decimal("2.5"),
                 extra_slippage_bps: Decimal = Decimal("5"),
                 commission_per_share: Decimal = Decimal("0"),
                 sec_fee_per_million: float = 27.80,
                 taf_per_share: float = 0.000166,
                 taf_cap_per_trade: float = 8.30):
        self.half_spread_bps = half_spread_bps
        self.extra_slippage_bps = extra_slippage_bps
        self.commission_per_share = commission_per_share
        self.sec_fee_per_million = sec_fee_per_million
        self.taf_per_share = taf_per_share
        self.taf_cap_per_trade = taf_cap_per_trade

    def fill_price(self, next_open: Decimal, side: Side) -> Decimal:
        adverse = (self.half_spread_bps + self.extra_slippage_bps) / Decimal("10000")
        sign = 1 if side is Side.BUY else -1
        return next_open * (1 + sign * adverse)

    def sell_fees(self, qty: float, proceeds: float) -> float:
        """Regulatory fees charged on a sell, invoiced-style (each component
        rounded up to the next cent). Buys carry no fees."""
        def cents_up(x: float) -> float:
            return math.ceil(x * 100 - 1e-9) / 100 if x > 0 else 0.0

        sec = cents_up(proceeds * self.sec_fee_per_million / 1_000_000)
        taf = cents_up(min(qty * self.taf_per_share, self.taf_cap_per_trade))
        return sec + taf
