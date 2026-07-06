"""Composite: runs multiple sleeves as one Strategy, so the engine, risk
manager, and gates see a single portfolio.

Routing rules:
  * each child sees only the positions it owns (Position.strategy);
    positions with unknown ownership go to the first child
  * exits always pass through; a buy is dropped if the symbol is already
    held by ANY sleeve or another sleeve claimed it this cycle (first child
    in config order wins ties)
  * risk limits stay global — the risk manager doesn't know or care how
    many sleeves want a slot, which is the point
"""
from __future__ import annotations

from core.models import Side, Signal
from strategies.base import Strategy


class CompositeStrategy(Strategy):
    name = "composite"

    def __init__(self, children: list[Strategy]):
        super().__init__({})
        if not children:
            raise ValueError("CompositeStrategy needs at least one child")
        self.children = children
        self.params = {c.name: c.params for c in children}

    def on_daily_close(self, view, positions):
        default_owner = self.children[0].name
        signals: list[Signal] = []
        claimed: set[str] = set(positions)
        for child in self.children:
            mine = {s: p for s, p in positions.items()
                    if (p.strategy or default_owner) == child.name}
            for sig in child.on_daily_close(view, mine):
                if sig.side is Side.BUY:
                    if sig.symbol in claimed:
                        continue
                    claimed.add(sig.symbol)
                signals.append(sig)
        return ([s for s in signals if s.side is Side.SELL]
                + [s for s in signals if s.side is Side.BUY])

    def warmup_bars(self) -> int:
        return max(c.warmup_bars() for c in self.children)
