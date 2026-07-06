"""Event-driven backtester over daily bars.

Deliberately custom and small instead of backtrader/vectorbt, because the
gate pipeline requires the EXACT strategy + risk-manager code that will trade
live to be what gets backtested. vectorbt is welcome for research sweeps;
gate-qualifying runs happen here.

Look-ahead prevention:
  * Strategies see history only through a clock-gated HistoryView — future
    bars are structurally unreachable (data/view.py).
  * Signals computed on day T's close fill at day T+1's OPEN through the cost
    model — never at T's close.

Realism model:
  * Fills adverse-adjusted by half-spread + slippage bps (backtest/costs.py).
  * Protective stops behave like broker-resting stop orders: they fire even
    while the risk manager has trading halted, and gaps through the stop fill
    at the open, not at the stop price.
  * Cash-account T+1 settlement: sale proceeds become settled cash the next
    session; the risk manager's settled-cash check sees this.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.costs import CostModel
from backtest.metrics import compute_metrics
from core.models import OrderIntent, Position, Side
from core.settings import Settings
from data.view import HistoryView
from risk.manager import HaltState, RiskManager
from strategies.base import Strategy


@dataclass
class Trade:
    symbol: str
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    qty: float
    entry_price: float
    exit_price: float
    pnl: float
    ret_pct: float
    bars_held: int
    exit_reason: str
    fees: float = 0.0           # regulatory sell fees (already deducted from pnl)


class BTPortfolio:
    """Portfolio state exposing exactly what RiskManager.approve reads."""

    def __init__(self, cash: float):
        self.settled_cash = cash
        self.unsettled_cash = 0.0
        self.positions: dict[str, Position] = {}
        self.bought_today: set[str] = set()
        self.pending_entries: set[str] = set()
        self._last_close: dict[str, float] = {}

    def new_day(self) -> None:
        self.settled_cash += self.unsettled_cash   # T+1: yesterday's sales settle
        self.unsettled_cash = 0.0
        self.bought_today.clear()

    def mark(self, closes: dict[str, float]) -> None:
        self._last_close.update(closes)

    @property
    def gross_exposure(self) -> float:
        return sum(
            float(p.qty) * self._last_close.get(s, float(p.avg_entry))
            for s, p in self.positions.items()
        )

    @property
    def equity(self) -> float:
        return self.settled_cash + self.unsettled_cash + self.gross_exposure


class BacktestEngine:
    def __init__(self, settings: Settings, strategy: Strategy, risk: RiskManager,
                 cost_model: CostModel | None = None):
        self.cfg = settings.raw
        self.strategy = strategy
        self.risk = risk
        bt = self.cfg["backtest"]
        self.costs = cost_model or CostModel(
            extra_slippage_bps=Decimal(str(bt["extra_slippage_bps"])),
            sec_fee_per_million=float(bt.get("sec_fee_per_million", 27.80)),
            taf_per_share=float(bt.get("taf_per_share", 0.000166)),
        )
        self.bucket_of = {
            s: b
            for b, syms in self.cfg["universe"]["buckets"].items()
            for s in syms
        }

    def run(self, bars: pd.DataFrame, start=None, end=None) -> "BacktestResult":
        frames = {str(s): df.droplevel(0).sort_index() for s, df in bars.groupby(level=0)}
        all_dates = sorted({ts for df in frames.values() for ts in df.index})
        start_ts = pd.Timestamp(start) if start else None
        end_ts = pd.Timestamp(end) if end else None
        dates = [d for d in all_dates
                 if (start_ts is None or d >= start_ts) and (end_ts is None or d <= end_ts)]

        pf = BTPortfolio(float(self.cfg["account"]["starting_capital"]))
        pending: list[OrderIntent] = []
        trades: list[Trade] = []
        equity_rows: list[tuple[pd.Timestamp, float]] = []
        max_bars = self.strategy.warmup_bars()
        # A live drawdown halt ends only with a written post-mortem. In a
        # backtest there is no operator, so simulate that documented policy:
        # resume after N sessions (docs/GATES.md forbids same-week restarts).
        # Trips are counted and reported — GATES requires they be rare.
        reset_after = int(self.cfg["backtest"].get("drawdown_reset_sessions", 10))
        dd_sessions = 0

        for t in dates:
            pf.new_day()
            self.risk.new_session(t)
            if self.risk.halt == HaltState.DRAWDOWN:
                dd_sessions += 1
                if dd_sessions >= reset_after:
                    self.risk.manual_reset(
                        f"backtest policy: simulated post-mortem after {reset_after} sessions")
                    dd_sessions = 0
            else:
                dd_sessions = 0
            today = {s: frames[s].loc[t] for s in frames if t in frames[s].index}

            # 1. Fill intents queued at yesterday's close, at today's open.
            for intent in pending:
                sym = intent.signal.symbol
                if sym not in today:
                    if intent.signal.side is Side.BUY:   # no bar: release reservation
                        pf.settled_cash += float(intent.notional)
                        pf.pending_entries.discard(sym)
                    continue
                row = today[sym]
                if intent.signal.side is Side.BUY:
                    fill_px = self._buy_fill_price(intent, row)
                    if fill_px is None:              # resting limit never touched
                        pf.settled_cash += float(intent.notional)
                        pf.pending_entries.discard(sym)
                        self.risk.audit.event("order_expired", symbol=sym,
                                              reason="limit not reached; entry lapsed")
                        continue
                    qty = float(intent.notional) / fill_px
                    pf.pending_entries.discard(sym)
                    pf.positions[sym] = Position(
                        symbol=sym, qty=Decimal(str(qty)),
                        avg_entry=Decimal(str(fill_px)),
                        stop_price=intent.signal.stop_price,
                        opened_at=t, bucket=self.bucket_of.get(sym, "other"),
                        strategy=intent.signal.strategy,
                    )
                    pf.bought_today.add(sym)
                elif sym in pf.positions:
                    self._exit(pf, sym, float(row["open"]), t, intent.signal.reason, trades)
            pending = []

            # 2. Broker-resting protective stops (fire even under halts).
            for sym, pos in list(pf.positions.items()):
                if pos.stop_price is None or sym not in today:
                    continue
                stop = float(pos.stop_price)
                row = today[sym]
                if float(row["open"]) <= stop:      # gapped through: fill at open
                    self._exit(pf, sym, float(row["open"]), t, "protective stop (gap)", trades)
                elif float(row["low"]) <= stop:
                    self._exit(pf, sym, stop, t, "protective stop", trades)

            # 3. Mark to market; breakers check on every mark.
            closes = {s: float(r["close"]) for s, r in today.items()}
            pf.mark(closes)
            self.risk.on_mark(t, pf.equity)
            equity_rows.append((t, pf.equity))

            # 4. Signals at the close -> risk gate -> queue for tomorrow's open.
            view = HistoryView(frames, asof=t, max_bars=max_bars)
            signals = self.strategy.on_daily_close(view, dict(pf.positions))
            ordered = ([s for s in signals if s.side is Side.SELL]
                       + [s for s in signals if s.side is Side.BUY])
            for sig in ordered:
                px = closes.get(sig.symbol)
                if px is None:
                    continue
                intent = self.risk.approve(sig, pf, price=px)
                if intent is None:
                    continue
                if sig.side is Side.BUY:            # reserve settled cash now
                    pf.settled_cash -= float(intent.notional)
                    pf.pending_entries.add(sig.symbol)
                pending.append(intent)

        equity = pd.Series(dict(equity_rows)).sort_index()
        open_positions = [
            {"symbol": s, "qty": float(p.qty), "avg_entry": float(p.avg_entry),
             "last_close": pf._last_close.get(s), "opened_at": str(p.opened_at)}
            for s, p in pf.positions.items()
        ]
        metrics = compute_metrics(equity, trades)
        metrics["drawdown_halts"] = self.risk.drawdown_trips
        return BacktestResult(equity, trades, metrics, self.cfg, open_positions)

    def _buy_fill_price(self, intent: OrderIntent, row) -> float | None:
        """Market buys fill at the open plus adverse costs. Resting-limit buys:
        marketable at the open -> open+costs capped at the limit; touched
        intraday -> exactly the limit (passive fill, you set the price);
        never touched -> None (DAY order lapses)."""
        open_px = Decimal(str(float(row["open"])))
        market_fill = float(self.costs.fill_price(open_px, Side.BUY))
        if intent.limit_price is None:
            return market_fill
        limit = float(intent.limit_price)
        if float(row["open"]) <= limit:
            return min(market_fill, limit)
        if float(row["low"]) <= limit:
            return limit
        return None

    def _exit(self, pf: BTPortfolio, sym: str, base_px: float, t: pd.Timestamp,
              reason: str, trades: list[Trade]) -> None:
        pos = pf.positions.pop(sym)
        fill_px = float(self.costs.fill_price(Decimal(str(base_px)), Side.SELL))
        qty, entry = float(pos.qty), float(pos.avg_entry)
        fees = self.costs.sell_fees(qty, qty * fill_px)
        pf.unsettled_cash += qty * fill_px - fees
        entry_ts = pd.Timestamp(pos.opened_at)
        trades.append(Trade(
            symbol=sym, entry_ts=entry_ts, exit_ts=t, qty=qty,
            entry_price=entry, exit_price=fill_px,
            pnl=(fill_px - entry) * qty - fees, ret_pct=fill_px / entry - 1,
            bars_held=int(np.busday_count(entry_ts.date(), t.date())),
            exit_reason=reason, fees=fees,
        ))


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: list[Trade]
    metrics: dict
    config: dict
    open_positions: list[dict] = None

    def save(self, out_dir) -> None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        self.equity.rename("equity").to_csv(out / "equity.csv")
        pd.DataFrame([asdict(t) for t in self.trades]).to_csv(out / "trades.csv", index=False)
        (out / "metrics.json").write_text(json.dumps(self.metrics, indent=2, default=str))
        (out / "config.json").write_text(json.dumps(self.config, indent=2, default=str))
