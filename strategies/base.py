"""Strategy interface. THE core contract of the system:

The SAME Strategy subclass runs unmodified in backtest, paper, and live.
It sees only (a) a clock-gated HistoryView of bars and (b) its own open
positions. It emits Signals — never orders, never sizes. If a strategy needs
something not expressible through this interface, extend the interface,
don't bypass it; that's what keeps backtest results meaningful for the gates.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from core.models import Position, Signal
from data.view import HistoryView


class Strategy(ABC):
    name: str = "unnamed"

    def __init__(self, params: dict):
        self.params = params

    @abstractmethod
    def on_daily_close(
        self, view: HistoryView, positions: dict[str, Position]
    ) -> list[Signal]:
        """Called once per trading day near the close (and once per bar in
        backtest). `view.history(symbol)` returns bars strictly up to and
        including the current bar — future data is structurally unreachable.
        Return entry/exit Signals; return [] to do nothing.
        """
        ...

    def warmup_bars(self) -> int:
        """How much history on_daily_close needs (e.g. 200 for a 200-SMA)."""
        return 250
