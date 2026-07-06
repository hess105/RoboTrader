"""Data layer interface. Backtest, paper, and live all consume this Protocol,
so strategies never know where bars come from (look-ahead prevention lives in
the backtest engine's clock, not here).
"""
from __future__ import annotations

from datetime import date
from typing import Protocol

import pandas as pd


class DataProvider(Protocol):
    def daily_bars(self, symbols: list[str], start: date, end: date) -> pd.DataFrame:
        """MultiIndex (symbol, ts) OHLCV frame. Adjusted for splits/dividends."""
        ...

    def latest_quote(self, symbol: str) -> tuple[float, float]:
        """(bid, ask). Used for spread checks and marketable-limit pricing."""
        ...
