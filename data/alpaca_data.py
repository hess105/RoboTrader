"""Alpaca market data (free IEX feed — sufficient for daily-bar swing
trading). Implements DataProvider. History requests go through BarCache
first; only missing ranges hit the API, which keeps the 200 req/min free-tier
limit irrelevant for a ~30-symbol universe.

Timestamps are normalized to tz-naive New York dates so daily bars align
across symbols and with the backtester's clock.
"""
from __future__ import annotations

import pandas as pd

from data.cache import BarCache

_COLS = ["open", "high", "low", "close", "volume"]


class AlpacaData:
    def __init__(self, key_id: str, secret_key: str, cache_dir: str = "data/cache"):
        from alpaca.data.historical import StockHistoricalDataClient

        self._client = StockHistoricalDataClient(key_id, secret_key)
        self.cache = BarCache(cache_dir)

    def daily_bars(self, symbols: list[str], start, end) -> pd.DataFrame:
        """MultiIndex (symbol, ts) OHLCV frame, split/dividend adjusted."""
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        frames: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            df = self.cache.get(sym, start, end)
            if df is None:
                cov = self.cache.coverage(sym)
                if cov and cov[0] <= start and cov[1] < end:
                    fetched = self._fetch(sym, cov[1] + pd.Timedelta(days=1), end)
                else:
                    fetched = self._fetch(sym, start, end)
                self.cache.put(sym, fetched, start, end)
                df = self.cache.get(sym, start, end)
            if df is not None and not df.empty:
                frames[sym] = df
        out = pd.concat(frames, names=["symbol", "ts"])
        return out.sort_index()

    def latest_quote(self, symbol: str) -> tuple[float, float]:
        from alpaca.data.requests import StockLatestQuoteRequest

        q = self._client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=symbol)
        )[symbol]
        return float(q.bid_price), float(q.ask_price)

    def _fetch(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        from alpaca.data.enums import Adjustment
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start.to_pydatetime(),
            end=end.to_pydatetime(),
            adjustment=Adjustment.ALL,
        )
        df = self._client.get_stock_bars(req).df
        if df.empty:
            return pd.DataFrame(columns=_COLS)
        df = df.droplevel(0)
        idx = pd.DatetimeIndex(df.index).tz_convert("America/New_York")
        df.index = idx.normalize().tz_localize(None)
        df.index.name = "ts"
        return df[_COLS].sort_index()
