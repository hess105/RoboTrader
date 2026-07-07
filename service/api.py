"""FastAPI control surface the GUI (and CLI) talk to. The engine enforces
everything; these endpoints only expose state and accept operator commands.
Bound to localhost by default — use Tailscale for phone access, never expose
the port directly.

Mode switching is NOT an API operation. Going live = restarting the engine
with --config config/live.yaml + the typed phrase on the engine's terminal.
A remote client that can flip real-money mode is a footgun by construction.
"""
from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.models import Mode
from service.backtest_jobs import runner as backtest_runner

GUI_DIST = Path(__file__).resolve().parent.parent / "gui" / "web" / "dist"
BACKTESTS_DIR = Path("journal/backtests")


class Note(BaseModel):
    note: str


class KillBody(BaseModel):
    confirm: str
    reason: str = "manual"


class KeysBody(BaseModel):
    mode: str
    key_id: str
    secret_key: str


class RunBacktestBody(BaseModel):
    start: str | None = None
    end: str | None = None


def create_app(engine) -> FastAPI:
    app = FastAPI(title="RoboTrader Engine", docs_url=None, redoc_url=None)

    # ------------------------------------------------------------ read side

    if (GUI_DIST / "index.html").exists():
        from fastapi.staticfiles import StaticFiles

        app.mount("/assets", StaticFiles(directory=GUI_DIST / "assets"), name="assets")

    @app.get("/")
    def index():
        if (GUI_DIST / "index.html").exists():
            return FileResponse(GUI_DIST / "index.html")
        return {"robotrader": "engine up",
                "gui": "not built — run `make gui-build`, or `make gui` for the dev server"}

    @app.get("/status")
    def status():
        return engine.status()

    @app.get("/account")
    def account():
        return engine.account_summary()

    @app.get("/positions")
    def positions():
        return engine.positions_view()

    @app.get("/orders")
    def orders(limit: int = 200):
        return engine.orders_view(limit)

    @app.get("/risk")
    def risk():
        return engine.risk.status()

    @app.get("/equity")
    def equity(days: int = 90):
        return engine.equity_history(days)

    @app.get("/gate2")
    def gate2():
        return engine.gate2_status()

    @app.get("/config")
    def config():
        # Merged base+mode yaml (account, universe, strategies, risk,
        # execution, monitoring, alerts) — no secrets live here, but the
        # live confirmation phrase is deliberately never surfaced to a
        # remote client, same reasoning as "no live-mode switch in the GUI".
        cfg = dict(engine.settings.raw)
        cfg.pop("live_confirmation_phrase", None)
        return cfg

    @app.post("/alerts/test")
    def alerts_test():
        engine.test_alerts()
        return {"sent": True}

    @app.get("/logs")
    def logs(kind: str | None = None, q: str | None = None, limit: int = 300):
        rows = engine.audit.query(kind or None, limit=limit)
        if q:
            needle = q.lower()
            rows = [r for r in rows
                    if needle in json.dumps(r, default=str).lower()]
        return rows

    @app.get("/backtests")
    def backtests():
        if not BACKTESTS_DIR.exists():
            return []
        out = []
        for d in sorted(BACKTESTS_DIR.iterdir(), reverse=True):
            metrics = d / "metrics.json"
            if metrics.exists():
                out.append({"run_id": d.name, **json.loads(metrics.read_text())})
        return out

    @app.get("/backtests/{run_id}")
    def backtest_detail(run_id: str):
        d = BACKTESTS_DIR / run_id
        if not d.is_dir() or d.parent != BACKTESTS_DIR:      # no traversal
            raise HTTPException(404, "unknown run")
        detail = {"run_id": run_id,
                  "metrics": json.loads((d / "metrics.json").read_text())}
        with open(d / "equity.csv") as fh:
            rows = list(csv.reader(fh))[1:]
        detail["equity"] = [[r[0], float(r[1])] for r in rows]
        trades_path = d / "trades.csv"
        if trades_path.exists():
            with open(trades_path) as fh:
                detail["trades"] = list(csv.DictReader(fh))[-200:]
        gate1_path = d / "gate1.json"
        if gate1_path.exists():
            detail.update(json.loads(gate1_path.read_text()))    # gate1, stress
        return detail

    @app.post("/backtests/run")
    def backtests_run(body: RunBacktestBody = RunBacktestBody()):
        # Heavy, synchronous CPU work (data fetch + the backtest loop) run in
        # a background thread — see service/backtest_jobs.py. Refused in LIVE
        # mode: that GIL contention competing with the live tick loop is
        # exactly the kind of downtime risk this project treats as unsafe
        # (protective stops are engine-monitored, not broker-resting).
        if engine.settings.mode is Mode.LIVE:
            raise HTTPException(
                403, "Backtests are disabled while the engine is in LIVE mode.")
        try:
            backtest_runner.start(body.start, body.end)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"started": True}

    @app.get("/backtests/run/status")
    def backtests_run_status():
        return backtest_runner.status()

    # --------------------------------------------------------- command side

    @app.post("/strategy/pause")
    def pause(body: Note):
        engine.set_paused(True, body.note)
        return {"paused": True}

    @app.post("/strategy/resume")
    def resume(body: Note):
        engine.set_paused(False, body.note)
        return {"paused": False}

    @app.post("/halt/reset")
    def halt_reset(body: Note):
        try:
            engine.risk.manual_reset(body.note)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"halt": engine.risk.halt}

    @app.post("/killswitch")
    def killswitch(body: KillBody):
        if body.confirm != "FLATTEN":
            raise HTTPException(400, 'confirmation token must be exactly "FLATTEN"')
        flat = engine.kill_switch.fire("api", body.reason)
        return {"flat": flat, "halt": engine.risk.halt}

    @app.post("/keys")
    def store_keys(body: KeysBody):
        if body.mode not in ("paper", "live"):
            raise HTTPException(400, "mode must be paper or live")
        import keyring

        prefix = body.mode.upper()
        keyring.set_password("robotrader", f"{prefix}_KEY_ID", body.key_id)
        keyring.set_password("robotrader", f"{prefix}_SECRET_KEY", body.secret_key)
        return {"stored": body.mode}                 # keys are never echoed back

    # ------------------------------------------------------------- streaming

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        try:
            while True:
                await socket.send_json(engine.status())
                await asyncio.sleep(2)
        except WebSocketDisconnect:
            pass

    return app
