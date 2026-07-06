"""Trend-pullback mean reversion on liquid ETFs/mega-caps (daily bars).

Long-only. Entry: close > 200-day SMA (per-symbol uptrend filter) AND RSI(2)
below the washout threshold, subject to three structural filters:

  * min_atr_pct — volatility floor: the expected bounce must be large enough
    to clear round-trip costs; low-ATR symbols (XLU-style) churn fees.
  * market_filter — regime gate: no new entries while the market proxy (SPY)
    is below its own long SMA; buying dips in a broad downtrend is knife-
    catching even when an individual symbol is still above its SMA.
  * entry_limit_atr — if > 0, entries rest as a DAY limit at
    (close - entry_limit_atr * ATR) instead of buying the next open: the
    position only opens if the washout deepens, which is where the mean-
    reversion edge concentrates. Unfilled limits lapse, costing nothing.

Exits: RSI(2) above exit_rsi_min, the ATR hard stop (engine/broker held),
or the time stop. Entry candidates rank most-washed-out first; the risk
manager, not the strategy, decides how many become orders.
"""
from __future__ import annotations

from decimal import Decimal

import pandas as pd

from core.models import Side, Signal
from strategies.base import Strategy
from strategies.indicators import atr, rsi, sma


class TrendPullback(Strategy):
    name = "trend_pullback"

    def on_daily_close(self, view, positions):
        p = self.params
        ts = view.asof
        signals: list[Signal] = []

        # Exits first — never blocked by entry-side filters.
        for sym, pos in positions.items():
            df = view.history(sym)
            if df.empty:
                continue
            r = rsi(df["close"], p["pullback_rsi_period"]).iloc[-1]
            held = int((df.index > pd.Timestamp(pos.opened_at)).sum())
            if r >= p["exit_rsi_min"]:
                signals.append(Signal(self.name, sym, Side.SELL,
                                      f"rsi_exit (RSI={r:.0f})", None, ts))
            elif held >= p["max_hold_days"]:
                signals.append(Signal(self.name, sym, Side.SELL,
                                      f"time_stop ({held} bars)", None, ts))

        if not self._market_ok(view, p):
            return signals                      # regime gate: exits only

        candidates: list[tuple[float, Signal]] = []
        for sym in view.symbols:
            if sym in positions:
                continue
            df = view.history(sym)
            if len(df) < p["trend_sma"] + 1:
                continue
            close = df["close"]
            last = float(close.iloc[-1])
            trend = sma(close, p["trend_sma"]).iloc[-1]
            r = rsi(close, p["pullback_rsi_period"]).iloc[-1]
            if pd.isna(trend) or pd.isna(r):
                continue
            if not (last > trend and r < p["pullback_rsi_max"]):
                continue
            a = float(atr(df["high"], df["low"], close, p["atr_period"]).iloc[-1])
            if pd.isna(a) or a <= 0:
                continue
            if a / last * 100 < float(p.get("min_atr_pct", 0)):
                continue                        # volatility floor: can't clear costs
            limit_atr = float(p.get("entry_limit_atr", 0))
            entry_est = last - limit_atr * a
            stop = entry_est - p["stop_atr_mult"] * a
            if stop <= 0:
                continue
            sig = Signal(
                self.name, sym, Side.BUY,
                f"pullback RSI={r:.1f} above SMA{p['trend_sma']}"
                + (f", limit {limit_atr}xATR below close" if limit_atr else ""),
                Decimal(str(round(stop, 4))), ts,
                limit_price=Decimal(str(round(entry_est, 4))) if limit_atr else None,
            )
            candidates.append((float(r), sig))
        candidates.sort(key=lambda c: c[0])
        signals.extend(sig for _, sig in candidates)
        return signals

    def _market_ok(self, view, p) -> bool:
        if not p.get("market_filter", False):
            return True
        proxy = p.get("market_filter_symbol", "SPY")
        if proxy not in view.symbols:
            return True
        df = view.history(proxy)
        if len(df) < p["trend_sma"]:
            return True
        trend = sma(df["close"], p["trend_sma"]).iloc[-1]
        return bool(not pd.isna(trend) and float(df["close"].iloc[-1]) > float(trend))

    def warmup_bars(self) -> int:
        return int(self.params["trend_sma"]) + 60
