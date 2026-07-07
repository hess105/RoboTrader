"""The engine daemon — the process that owns ALL trading logic, risk checks,
and the kill switch. Runs headless; the GUI is a disposable browser client.

Startup sequence (order matters):
  1. load_settings (live mode => interactive confirmation phrase)
  2. journal ENGINE_START; OrderManager.recover_pending() resolves any
     crash-time orders
  3. Reconciler.run() — on mismatch, RECONCILE halt (entries blocked) until
     an operator resets with a note
  4. scheduler jobs (all times America/New_York, weekdays):
       16:15  compute signals on COMPLETED daily bars -> risk gate -> journal
              intents as status 'queued_open' (matches the backtest: signal
              at close T, fill at open T+1)
       09:35  submit queued intents (entries re-checked against current halt
              state; overnight breaker trips cancel them)
       09:00  reconcile + clear day-trade ledger
       every minute 09:00-16:59  tick: health, equity mark -> breakers,
              fill sync, engine-side protective stop monitor
       16:20  daily summary alert
  5. serve control API + GUI on 127.0.0.1:8765 (Tailscale for phone access;
     never expose the port directly)

Protective stops are ENGINE-MONITORED, not broker-resting: Alpaca does not
support stop orders on fractional shares. Consequence: engine downtime means
positions are unprotected — hence HealthMonitor's kill-on-reconnect policy
and the healthchecks.io dead-man ping.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd

from core.models import OrderIntent, Position, Side, Signal
from core.settings import Settings, broker_creds, load_settings
from data.alpaca_data import AlpacaData
from data.view import HistoryView
from execution.alpaca_client import AlpacaExecution
from execution.order_manager import OrderManager
from execution.reconciler import Reconciler
from journal.audit import AuditLog
from monitoring.alerts import Alerter
from monitoring.health import HealthMonitor
from risk.kill_switch import KillSwitch
from risk.manager import HaltState, RiskManager
from strategies import build_strategy

NY = ZoneInfo("America/New_York")


class PortfolioState:
    """Live portfolio view exposing exactly what RiskManager.approve reads."""

    def __init__(self):
        self.equity = 0.0
        self.settled_cash = 0.0
        self.gross_exposure = 0.0
        self.positions: dict[str, Position] = {}
        self.bought_today: set[str] = set()
        self.pending_entries: set[str] = set()


class TradingEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.cfg = settings.raw
        self.audit = AuditLog(self.cfg["logging"]["audit_db"])
        self.alerts = Alerter(self.cfg)
        self.risk = RiskManager(self.cfg, self.audit, self.alerts)
        creds = broker_creds(settings.mode)
        self.broker = AlpacaExecution(creds, settings.mode)
        self.data = AlpacaData(creds.key_id.get_secret_value(),
                               creds.secret_key.get_secret_value())
        self.om = OrderManager(self.broker, self.audit, self.alerts)
        self.kill_switch = KillSwitch(self.broker, self.risk, self.audit, self.alerts)
        self.recon = Reconciler(self.broker, self.audit, self.alerts)
        mon = self.cfg.get("monitoring", {})
        self.health = HealthMonitor(
            self.alerts,
            disconnect_alert_min=int(mon.get("disconnect_alert_min", 5)),
            kill_after_min=int(mon.get("kill_after_disconnect_min", 15)),
        )
        self.strategy = build_strategy(self.cfg)
        self.universe = [s for ss in self.cfg["universe"]["buckets"].values() for s in ss]
        self.bucket_of = {s: b for b, ss in self.cfg["universe"]["buckets"].items() for s in ss}
        self.portfolio = PortfolioState()
        self.paused = False
        self.market_open = False
        self.started_at = datetime.now(timezone.utc)
        self.scheduler = None

    # ------------------------------------------------------------- lifecycle

    def startup(self) -> None:
        self.audit.event("engine_start", detail=self.settings.mode.value)
        recovered = self.om.recover_pending()
        if recovered:
            self.alerts.send("WARN", "engine",
                             f"recovered {len(recovered)} in-flight order(s) at startup")
        self.refresh_portfolio()
        report = self.recon.run()
        if not report.clean:
            self.risk.engage(HaltState.RECONCILE,
                             f"startup reconcile: {'; '.join(report.mismatches)}")
        for sym, pos in self.portfolio.positions.items():
            if pos.stop_price is None:
                self.alerts.send("WARN", "risk",
                                 f"position {sym} has no protective stop on record")

    def start_scheduler(self) -> None:
        from apscheduler.schedulers.background import BackgroundScheduler

        s = BackgroundScheduler(timezone=NY)
        wd = {"day_of_week": "mon-fri"}
        s.add_job(self.compute_signals, "cron", hour=16, minute=15, **wd)
        s.add_job(self.submit_queued, "cron", hour=9, minute=35, **wd)
        s.add_job(self.reconcile_job, "cron", hour=9, minute=0, **wd)
        s.add_job(self.tick, "cron", hour="9-16", minute="*", **wd)
        s.add_job(self.daily_summary, "cron", hour=16, minute=20, **wd)
        s.start()
        self.scheduler = s

    # ------------------------------------------------------------------ jobs

    def compute_signals(self) -> None:
        """16:15 ET: completed daily bars -> signals -> risk gate -> queue for
        tomorrow's open. Mirrors the backtest's close-T/fill-T+1 semantics."""
        if self.paused or self.risk.halt == HaltState.KILL_SWITCH:
            self.audit.event("signals_skipped", reason=f"paused={self.paused} halt={self.risk.halt}")
            return
        end = datetime.now(NY).date()
        start = (pd.Timestamp(end) - pd.Timedelta(days=550)).date()
        bars = self.data.daily_bars(self.universe, start, end)
        frames = {str(s): df.droplevel(0).sort_index() for s, df in bars.groupby(level=0)}
        asof = max(df.index.max() for df in frames.values())
        self.refresh_portfolio()
        view = HistoryView(frames, asof, self.strategy.warmup_bars())
        signals = self.strategy.on_daily_close(view, dict(self.portfolio.positions))
        queued = 0
        for sig in ([s for s in signals if s.side is Side.SELL]
                    + [s for s in signals if s.side is Side.BUY]):
            df = frames.get(sig.symbol)
            if df is None:
                continue
            price = float(df["close"].iloc[-1])
            spread_pct = self._spread_pct(sig.symbol)
            intent = self.risk.approve(sig, self.portfolio, price, spread_pct)
            if intent is None:
                continue
            self.audit.upsert_order(
                intent.client_order_id, symbol=sig.symbol, side=sig.side.value,
                notional=float(intent.notional) if intent.notional else None,
                qty=float(intent.qty) if intent.qty else None, status="queued_open")
            self.audit.event("order_queued", symbol=sig.symbol, side=sig.side.value,
                             order_id=intent.client_order_id, reason=sig.reason)
            if sig.side is Side.BUY:
                self.portfolio.pending_entries.add(sig.symbol)
                if sig.stop_price is not None:
                    self.audit.event("stop_set", symbol=sig.symbol,
                                     price=float(sig.stop_price),
                                     reason=sig.strategy)
            queued += 1
        self.audit.event("signals_run", detail=f"asof={asof.date()} queued={queued}")

    def submit_queued(self) -> None:
        """09:35 ET: submit yesterday's queued intents. Entries re-check the
        halt state — an overnight breaker trip cancels them, exits proceed."""
        self.portfolio.bought_today.clear()
        self.refresh_portfolio()
        for row in self.audit.orders_by_status("queued_open"):
            cid, side = row["client_order_id"], Side(row["side"])
            if side is Side.BUY and self.risk.halt != HaltState.NONE:
                self.audit.upsert_order(cid, status="cancelled")
                self.audit.event("order_cancelled", order_id=cid,
                                 reason=f"halted at open: {self.risk.halt}")
                self.portfolio.pending_entries.discard(row["symbol"])
                continue
            intent = OrderIntent(
                client_order_id=cid,
                signal=Signal(self.strategy.name, row["symbol"], side,
                              "queued at close, submitted at open", None,
                              datetime.now(timezone.utc)),
                notional=Decimal(str(row["notional"])) if row["notional"] else None,
                qty=Decimal(str(row["qty"])) if row["qty"] else None,
                limit_price=None)
            self.om.execute(intent)
            if side is Side.BUY:
                self.portfolio.bought_today.add(row["symbol"])
        self.refresh_portfolio()

    def tick(self) -> None:
        """Every minute in market hours: health, breakers, fills, stops."""
        try:
            self.market_open = self.broker.market_is_open()
            self.refresh_portfolio()
        except Exception as exc:                     # noqa: BLE001 — connectivity failure
            self.health.record_failure(bool(self.portfolio.positions), str(exc))
            return
        if self.health.record_success():
            self.kill_switch.fire("health", "reconnect after prolonged outage "
                                            "with engine-side stops blind")
            return
        self.alerts.heartbeat()
        if not self.market_open:
            return
        self.risk.on_mark(datetime.now(timezone.utc), self.portfolio.equity)
        self._sync_fills()
        self._check_stops()

    def reconcile_job(self) -> None:
        report = self.recon.run()
        if not report.clean:
            self.risk.engage(HaltState.RECONCILE, "; ".join(report.mismatches))
        self.portfolio.bought_today.clear()

    def daily_summary(self) -> None:
        r = self.risk.last or {}
        self.alerts.send("INFO", "summary",
                         f"equity ${self.portfolio.equity:.2f} | day {-(r.get('day_loss_pct') or 0):+.2f}% "
                         f"| dd {r.get('drawdown_pct', 0):.2f}% | positions {len(self.portfolio.positions)} "
                         f"| halt {self.risk.halt}")

    # -------------------------------------------------------------- plumbing

    def refresh_portfolio(self) -> None:
        equity, cash, _bp = self.broker.account_equity()
        pf = self.portfolio
        pf.equity, pf.settled_cash = float(equity), float(cash)
        positions: dict[str, Position] = {}
        for p in self.broker.positions():
            p.bucket = self.bucket_of.get(p.symbol, "other")
            stop = self.audit.latest_stop(p.symbol)
            if stop is not None:
                p.stop_price = Decimal(str(stop["price"]))
                p.opened_at = datetime.fromisoformat(stop["ts"])
                p.strategy = stop.get("reason") or ""
            positions[p.symbol] = p
        pf.positions = positions
        pf.pending_entries &= {r["symbol"] for r in self.audit.orders_by_status("queued_open")}
        pf.gross_exposure = max(pf.equity - pf.settled_cash, 0.0)

    def _check_stops(self) -> None:
        for sym, pos in list(self.portfolio.positions.items()):
            if pos.stop_price is None:
                continue
            try:
                bid, _ask = self.data.latest_quote(sym)
            except Exception:                        # noqa: BLE001 — next tick retries
                continue
            if bid and bid <= float(pos.stop_price):
                sig = Signal(self.strategy.name, sym, Side.SELL,
                             f"protective stop {float(pos.stop_price):.2f} hit (bid {bid:.2f})",
                             None, datetime.now(timezone.utc))
                intent = self.risk.approve(sig, self.portfolio, bid)
                if intent is not None:
                    self.om.execute(intent)
                    self.portfolio.positions.pop(sym, None)

    def _sync_fills(self) -> None:
        from core.models import Fill, OrderStatus
        for row in (self.audit.orders_by_status("submitted")
                    + self.audit.orders_by_status("partially_filled")):
            order = self.broker.get_order_by_client_id(row["client_order_id"])
            if order is None or order.status.value == row["status"]:
                continue
            self.om.record(order)
            if order.status is OrderStatus.FILLED and order.avg_fill_price:
                self.om.on_fill(Fill(
                    broker_order_id=order.broker_order_id or "",
                    symbol=order.symbol, side=order.side,
                    qty=order.filled_qty, price=order.avg_fill_price,
                    ts=order.updated_at))

    def _spread_pct(self, symbol: str) -> float | None:
        try:
            bid, ask = self.data.latest_quote(symbol)
            mid = (bid + ask) / 2
            return (ask - bid) / mid * 100 if mid else None
        except Exception:                            # noqa: BLE001
            return None

    # ---------------------------------------------------------- API surface

    def status(self) -> dict:
        r = self.risk.last or {}
        return {
            "mode": self.settings.mode.value,
            "halt": self.risk.halt,
            "paused": self.paused,
            "market_open": self.market_open,
            "uptime_sec": int((datetime.now(timezone.utc) - self.started_at).total_seconds()),
            "equity": self.portfolio.equity,
            "day_loss_pct": r.get("day_loss_pct"),
            "drawdown_pct": r.get("drawdown_pct"),
            "open_positions": len(self.portfolio.positions),
            "health": self.health.status(),
            "strategy": {"name": self.strategy.name,
                         "params": self.strategy.params},
        }

    def account_summary(self) -> dict:
        r = self.risk.last or {}
        return {"equity": self.portfolio.equity, "settled_cash": self.portfolio.settled_cash,
                "gross_exposure": self.portfolio.gross_exposure,
                "peak_equity": self.risk.peak_equity, **r}

    def positions_view(self) -> list[dict]:
        try:
            rows = self.broker.positions_detail()
        except Exception:                            # noqa: BLE001 — serve cached view
            rows = [{"symbol": s, "qty": float(p.qty), "avg_entry": float(p.avg_entry)}
                    for s, p in self.portfolio.positions.items()]
        for row in rows:
            pos = self.portfolio.positions.get(row["symbol"])
            row["stop"] = float(pos.stop_price) if pos and pos.stop_price else None
            row["bucket"] = self.bucket_of.get(row["symbol"], "other")
            row["strategy"] = pos.strategy if pos else ""
        return rows

    def orders_view(self, limit: int = 200) -> list[dict]:
        return self.audit.orders_recent(limit)

    def equity_history(self, days: int = 90) -> list[tuple[str, float]]:
        return self.audit.daily_equity(days)

    def gate2_status(self) -> dict:
        """Progress against the Gate 2 checklist (docs/GATES.md): >=60 paper
        trading days AND >=30 closed trades, two kill-switch drills, and a
        verified end-to-end alert. Counted from the journal, not memory."""
        days = len(self.audit.daily_equity(10_000))
        sells = [f for f in self.audit.query("fill", limit=100_000)
                 if f.get("side") == "sell"]
        drills = self.audit.query("kill_switch", limit=100)
        alert_tests = self.audit.query("alert_test", limit=10)
        return {
            "trading_days": days, "target_days": 60,
            "closed_trades": len(sells), "target_trades": 30,
            "drills": len(drills), "target_drills": 2,
            "last_drill": drills[0]["ts"] if drills else None,
            "last_alert_test": alert_tests[0]["ts"] if alert_tests else None,
        }

    def test_alerts(self) -> None:
        self.audit.event("alert_test", reason="manual end-to-end alert test")
        self.alerts.send("INFO", "test",
                         "alert test — if you can read this on your phone, "
                         "the channel works")

    def set_paused(self, paused: bool, note: str) -> None:
        self.paused = paused
        self.audit.event("strategy_paused" if paused else "strategy_resumed", reason=note)
        self.alerts.send("INFO", "strategy",
                         f"{'paused' if paused else 'resumed'}: {note}")


def main(config_path: str | None = None) -> None:
    import uvicorn

    from monitoring.log_buffer import install as install_log_buffer
    from service.api import create_app

    install_log_buffer()          # GUI's Processes tab live-tails stdout/stderr
    settings = load_settings(config_path)
    engine = TradingEngine(settings)
    engine.startup()
    engine.start_scheduler()
    svc = settings.raw.get("service", {})
    print(f"RoboTrader engine up — mode={settings.mode.value} "
          f"dashboard=http://{svc.get('host', '127.0.0.1')}:{svc.get('port', 8765)}")
    uvicorn.run(create_app(engine), host=svc.get("host", "127.0.0.1"),
                port=int(svc.get("port", 8765)), log_level="warning")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--config", default=None,
                   help="config/paper.yaml or config/live.yaml")
    main(p.parse_args().config)
