"""Clock-gated view of bar history — the structural look-ahead guard.

Strategies receive a HistoryView, never a raw DataFrame. history() can only
return bars at or before `asof`, so future data is unreachable by
construction rather than by convention. The backtest engine advances `asof`
bar by bar; in paper/live, `asof` is simply the latest completed bar.
"""
from __future__ import annotations

import pandas as pd


class HistoryView:
    def __init__(self, frames: dict[str, pd.DataFrame], asof, max_bars: int | None = None):
        self._frames = frames
        self.asof = pd.Timestamp(asof)
        self.max_bars = max_bars

    @property
    def symbols(self) -> list[str]:
        return list(self._frames)

    def history(self, symbol: str) -> pd.DataFrame:
        """OHLCV bars for `symbol` with ts <= asof, most recent `max_bars` rows."""
        df = self._frames[symbol]
        i = df.index.searchsorted(self.asof, side="right")
        out = df.iloc[:i]
        return out.tail(self.max_bars) if self.max_bars else out
