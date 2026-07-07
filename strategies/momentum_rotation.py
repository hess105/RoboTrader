"""Momentum rotation sleeve (weeks-to-months holds, weekly rebalance).

Complements trend_pullback: mean reversion earns in choppy uptrends and sits
out sustained trends; momentum earns in sustained trends (2017 tech, 2022
energy) — the regimes where the pullback sleeve goes flat.

Logic, evaluated only on the first bar of each ISO week (deterministic from
the calendar — no hidden state, so live and backtest agree):
  * score = mean of `score_fast`- and `score_slow`-day returns
  * exit a holding when it falls below `exit_rank` in the score table or its
    score turns non-positive (absolute-momentum filter: never long a
    downtrend just because it's the least-bad option)
  * enter the top `top_n` symbols with positive score, not already held
  * protective stop at entry: stop_atr_mult * ATR below the close — wider
    than the pullback sleeve's, because trends need room to breathe

Buys strength at the next open (no resting limit: momentum entries chase,
they don't fish). Universe comes from params["symbols"], resolved from
config buckets by strategies.build_strategy.
"""
from __future__ import annotations

from decimal import Decimal

import pandas as pd

from core.models import Side, Signal
from strategies.base import Strategy
from strategies.indicators import atr


class MomentumRotation(Strategy):
    name = "momentum_rotation"

    def on_daily_close(self, view, positions):
        p = self.params
        ts = view.asof
        symbols = [s for s in p.get("symbols") or view.symbols if s in view.symbols]
        if not self._is_rebalance_day(view, symbols):
            return []

        slow = int(p["score_slow"])
        scores: dict[str, float] = {}
        for sym in symbols:
            close = view.history(sym)["close"]
            if len(close) < slow + 1:
                continue
            fast_r = float(close.iloc[-1] / close.iloc[-1 - int(p["score_fast"])] - 1)
            slow_r = float(close.iloc[-1] / close.iloc[-1 - slow] - 1)
            scores[sym] = (fast_r + slow_r) / 2
        if not scores:
            return []
        ranked = sorted(scores, key=scores.get, reverse=True)
        rank_of = {sym: i + 1 for i, sym in enumerate(ranked)}

        signals: list[Signal] = []
        for sym in positions:
            score = scores.get(sym)
            rank = rank_of.get(sym)
            if score is None or score <= float(p["min_score"]):
                signals.append(Signal(self.name, sym, Side.SELL,
                                      f"momo_exit score<=0 ({score})", None, ts))
            elif rank > int(p["exit_rank"]):
                signals.append(Signal(self.name, sym, Side.SELL,
                                      f"momo_exit rank {rank}", None, ts))

        for sym in ranked[: int(p["top_n"])]:
            if sym in positions or scores[sym] <= float(p["min_score"]):
                continue
            df = view.history(sym)
            last = float(df["close"].iloc[-1])
            a = float(atr(df["high"], df["low"], df["close"], p["atr_period"]).iloc[-1])
            stop = last - float(p["stop_atr_mult"]) * a
            if pd.isna(a) or stop <= 0:
                continue
            signals.append(Signal(
                self.name, sym, Side.BUY,
                f"momo_entry rank {rank_of[sym]} score {scores[sym]:.3f}",
                Decimal(str(round(stop, 4))), ts))
        return signals

    def explain(self, view, positions):
        p = self.params
        symbols = [s for s in p.get("symbols") or view.symbols if s in view.symbols]
        slow = int(p["score_slow"])
        scores: dict[str, float] = {}
        for sym in symbols:
            close = view.history(sym)["close"]
            if len(close) < slow + 1:
                continue
            fast_r = float(close.iloc[-1] / close.iloc[-1 - int(p["score_fast"])] - 1)
            slow_r = float(close.iloc[-1] / close.iloc[-1 - slow] - 1)
            scores[sym] = (fast_r + slow_r) / 2
        ranked = sorted(scores, key=scores.get, reverse=True)
        rank_of = {sym: i + 1 for i, sym in enumerate(ranked)}
        rebalance_today = self._is_rebalance_day(view, symbols)

        rows = []
        for sym in symbols:
            held = sym in positions
            score = scores.get(sym)
            rank = rank_of.get(sym)
            if score is None:
                rows.append({"symbol": sym, "strategy": self.name, "held": held,
                             "would_buy": False, "note": "insufficient history"})
                continue
            in_top = rank is not None and rank <= int(p["top_n"])
            above_min = score > float(p["min_score"])
            would_buy = bool(in_top and above_min and not held)
            if held:
                note = f"held, rank {rank}, score {score:.3f} (exits below score 0 or rank > {p['exit_rank']})"
            elif would_buy:
                note = f"BUY candidate: rank {rank} of {len(ranked)}, score {score:.3f}"
            elif not above_min:
                note = f"score {score:.3f} <= {p['min_score']} (absolute-momentum filter)"
            else:
                note = f"rank {rank} of {len(ranked)}, outside top {p['top_n']}"
            if not rebalance_today:
                note += " — not a rebalance day, evaluated for reference only"
            rows.append({
                "symbol": sym, "strategy": self.name, "close": None,
                "score": score, "rank": rank, "held": held,
                "would_buy": would_buy, "note": note,
            })
        return rows

    def _is_rebalance_day(self, view, symbols) -> bool:
        """True on the first trading day of an ISO week."""
        for sym in symbols:
            idx = view.history(sym).index
            if len(idx) >= 2:
                prev, cur = idx[-2], idx[-1]
                return tuple(cur.isocalendar())[:2] != tuple(prev.isocalendar())[:2]
        return False

    def warmup_bars(self) -> int:
        return int(self.params["score_slow"]) + 40
