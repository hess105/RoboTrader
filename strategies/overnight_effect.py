"""Overnight effect: buy a diversified basket of liquid large-caps at the
close, sell the whole basket at the next session's open. Captures the
well-documented overnight anomaly (close-to-open historically dominates
total return; see e.g. Lou/Polk/Skouras "A Tug of War") while sidestepping
intraday exposure entirely.

Long-only, single-session holds by construction. This strategy only ever
emits BUY signals — it never computes an exit. Every position it opens is
force-closed at the very next session's open by the ENGINE (see
`overnight_only` on the base class and its handling in
backtest/engine.py / service/engine.py), not by a strategy decision. That's
deliberate: there is nothing to decide on the exit side (the hold is always
exactly one session), so keeping exit logic out of the strategy removes an
entire class of "what if it holds too long" bugs.

Ranking signal — intraday-reversal score, computable from a single daily
bar (no intraday data needed):

    score = -(close/open - 1) / (atr_pct/100)

How many ATRs a name sold off *during today's own session*, sign-flipped:
the biggest intraday washouts rank first, on the bet that part of an
outsized intraday selloff reverses overnight. Candidates need score > 0
(no forced buying on days nothing sold off) and must clear liquidity/price/
volatility floors before ranking.

Every BUY signal still carries a stop_price, because risk.approve() sizes
off the entry-to-stop distance. This stop is a SIZING ANCHOR ONLY: the
position is never open during a monitored trading session (bought after
today's close, force-sold at tomorrow's open, zero ticks in between), so it
structurally can never fire as a real protective exit. The actual risk
control for this strategy is diversification across many small positions,
not a monitored stop.
"""
from __future__ import annotations

from decimal import Decimal

import pandas as pd

from core.models import Side, Signal
from strategies.base import Strategy
from strategies.indicators import atr


class OvernightEffect(Strategy):
    name = "overnight_effect"
    overnight_only = True

    def on_daily_close(self, view, positions):
        p = self.params
        ts = view.asof
        candidates = self._score_candidates(view)
        top_n = int(p["top_n"])
        signals: list[Signal] = []
        for rank, (sym, c) in enumerate(candidates, start=1):
            if rank > top_n:
                break
            if sym in positions:
                continue
            stop = c["close"] - float(p["stop_atr_mult"]) * c["atr"]
            if stop <= 0:
                continue
            signals.append(Signal(
                self.name, sym, Side.BUY,
                f"overnight rank {rank} score {c['score']:.2f} "
                f"(intraday {c['intraday_ret_pct']:+.2f}%)",
                Decimal(str(round(stop, 4))), ts,
            ))
        return signals

    def explain(self, view, positions):
        p = self.params
        candidates = self._score_candidates(view)
        top_n = int(p["top_n"])
        ranked = {sym: i + 1 for i, (sym, _) in enumerate(candidates)}
        scored = {sym: c for sym, c in candidates}
        symbols = [s for s in p.get("symbols") or view.symbols if s in view.symbols]
        rows = []
        for sym in symbols:
            df = view.history(sym)
            held = sym in positions
            if len(df) < self._min_history():
                rows.append({"symbol": sym, "strategy": self.name, "held": held,
                             "would_buy": False, "note": "insufficient history"})
                continue
            c = scored.get(sym)
            rank = ranked.get(sym)
            in_top = rank is not None and rank <= top_n
            would_buy = bool(in_top and not held)
            if held:
                note = "held overnight — force-sold at the next open regardless of score"
            elif c is None:
                note = "filtered out (price/liquidity/volatility floor, or score <= 0)"
            elif would_buy:
                note = f"BUY candidate: rank {rank}, sold off {c['intraday_ret_pct']:.2f}% today"
            else:
                note = f"rank {rank} of {len(candidates)}, outside top {top_n}"
            rows.append({
                "symbol": sym, "strategy": self.name,
                "close": c["close"] if c else float(df["close"].iloc[-1]),
                "open": c["open"] if c else None,
                "intraday_ret_pct": c["intraday_ret_pct"] if c else None,
                "atr_pct": c["atr_pct"] if c else None,
                "score": c["score"] if c else None,
                "rank": rank, "held": held, "would_buy": would_buy, "note": note,
            })
        return rows

    def _score_candidates(self, view) -> list[tuple[str, dict]]:
        p = self.params
        min_price = float(p.get("min_price", 0))
        min_atr_pct = float(p.get("min_atr_pct", 0))
        min_dollar_vol = float(p.get("min_avg_dollar_volume", 0))
        dv_lookback = int(p.get("dollar_volume_lookback", 20))
        symbols = [s for s in p.get("symbols") or view.symbols if s in view.symbols]
        scored: list[tuple[str, dict]] = []
        for sym in symbols:
            df = view.history(sym)
            if len(df) < self._min_history():
                continue
            row = df.iloc[-1]
            close, open_ = float(row["close"]), float(row["open"])
            if close < min_price or open_ <= 0:
                continue
            if min_dollar_vol > 0:
                recent = df.tail(dv_lookback)
                avg_dollar_vol = float((recent["close"] * recent["volume"]).mean())
                if pd.isna(avg_dollar_vol) or avg_dollar_vol < min_dollar_vol:
                    continue
            a = float(atr(df["high"], df["low"], df["close"], int(p["atr_period"])).iloc[-1])
            if pd.isna(a) or a <= 0:
                continue
            atr_pct = a / close * 100
            if atr_pct < min_atr_pct:
                continue
            intraday_ret_pct = (close / open_ - 1) * 100
            score = -intraday_ret_pct / atr_pct
            if score <= 0:
                continue
            scored.append((sym, {
                "close": close, "open": open_, "atr": a, "atr_pct": atr_pct,
                "intraday_ret_pct": intraday_ret_pct, "score": score,
            }))
        scored.sort(key=lambda item: item[1]["score"], reverse=True)
        return scored

    def _min_history(self) -> int:
        return max(int(self.params["atr_period"]) + 1,
                   int(self.params.get("dollar_volume_lookback", 20)))

    def warmup_bars(self) -> int:
        return self._min_history() + 5
