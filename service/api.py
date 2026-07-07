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
import math
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from core.models import Mode
from monitoring.log_buffer import clear as clear_log_buffer
from monitoring.log_buffer import recent as recent_logs
from monitoring.process_status import process_status
from service.backtest_jobs import runner as backtest_runner
from service.sweep_jobs import runner as sweep_runner

GUI_DIST = Path(__file__).resolve().parent.parent / "gui" / "web" / "dist"
BACKTESTS_DIR = Path("journal/backtests")
SWEEPS_DIR = Path("journal/sweeps")


def _json_safe(obj):
    """inf/-inf/nan are legitimate values here (e.g. profit_factor with zero
    losing trades, backtest/metrics.py), but plain JSON has no representation
    for them — Starlette's default JSONResponse renders with allow_nan=False
    and raises ValueError on them. Applied globally (see SafeJSONResponse)
    rather than patching every endpoint that could return a metrics dict."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


class SafeJSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return super().render(_json_safe(content))


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
    label: str | None = None


class RunSweepBody(BaseModel):
    n_samples: int = 250
    workers: int = 5
    is_end: str = "2021-12-31"
    oos_start: str = "2022-01-01"
    seed: int = 0
    label: str | None = None


def _safe_subdir(base: Path, name: str) -> Path:
    """Resolves name under base and refuses path traversal — shared by every
    GET/DELETE that takes a run_id straight from the URL."""
    d = base / name
    if not d.is_dir() or d.parent != base:
        raise HTTPException(404, "unknown run")
    return d


def create_app(engine) -> FastAPI:
    app = FastAPI(title="RoboTrader Engine", docs_url=None, redoc_url=None,
                 default_response_class=SafeJSONResponse)

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
                row = {"run_id": d.name, **json.loads(metrics.read_text())}
                meta = d / "meta.json"
                if meta.exists():
                    row["label"] = json.loads(meta.read_text()).get("label")
                out.append(row)
        return out

    @app.get("/backtests/{run_id}")
    def backtest_detail(run_id: str):
        d = _safe_subdir(BACKTESTS_DIR, run_id)
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
        meta_path = d / "meta.json"
        if meta_path.exists():
            detail["label"] = json.loads(meta_path.read_text()).get("label")
        return detail

    @app.delete("/backtests/{run_id}")
    def backtest_delete(run_id: str):
        # The directory is only written once, atomically, at the very end
        # of a run (result.save() + gate1.json), so there's no in-progress
        # run to protect against here — a run_id that's still computing
        # simply doesn't have a directory yet, and _safe_subdir 404s on it.
        d = _safe_subdir(BACKTESTS_DIR, run_id)
        shutil.rmtree(d)
        return {"deleted": run_id}

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
        if sweep_runner.is_running():
            raise HTTPException(409, "A sweep is already running.")
        try:
            backtest_runner.start(body.start, body.end, body.label)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"started": True}

    @app.get("/backtests/run/status")
    def backtests_run_status():
        return backtest_runner.status()

    @app.post("/backtests/run/stop")
    def backtests_run_stop():
        stopped = backtest_runner.stop()
        return {"stopped": stopped}

    @app.get("/sweeps")
    def sweeps():
        if not SWEEPS_DIR.exists():
            return []
        out = []
        for d in sorted(SWEEPS_DIR.iterdir(), reverse=True):
            f = d / "results.json"
            if f.exists():
                data = json.loads(f.read_text())
                out.append({"run_id": d.name, "label": data.get("label"),
                            "eligible": data.get("eligible"),
                            "n_samples": data.get("n_samples"),
                            "full_grid_size": data.get("full_grid_size"),
                            "profitable_folds": data.get("profitable_folds"),
                            "overfit_warning": data.get("overfit_warning")})
        return out

    @app.get("/sweeps/{run_id}")
    def sweep_detail(run_id: str):
        d = _safe_subdir(SWEEPS_DIR, run_id)
        return json.loads((d / "results.json").read_text())

    @app.delete("/sweeps/{run_id}")
    def sweep_delete(run_id: str):
        d = _safe_subdir(SWEEPS_DIR, run_id)
        shutil.rmtree(d)
        return {"deleted": run_id}

    @app.post("/sweeps/run")
    def sweeps_run(body: RunSweepBody = RunSweepBody()):
        # Same LIVE-mode and mutual-exclusion guards as /backtests/run — a
        # sweep is dozens of backtests back to back, even more GIL/CPU
        # contention than a single one.
        if engine.settings.mode is Mode.LIVE:
            raise HTTPException(
                403, "Sweeps are disabled while the engine is in LIVE mode.")
        if backtest_runner.is_running():
            raise HTTPException(409, "A backtest is already running.")
        try:
            sweep_runner.start(body.n_samples, body.workers, body.is_end,
                                body.oos_start, body.seed, body.label)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return {"started": True}

    @app.get("/sweeps/run/status")
    def sweeps_run_status():
        return sweep_runner.status()

    @app.post("/sweeps/run/stop")
    def sweeps_run_stop():
        stopped = sweep_runner.stop()
        return {"stopped": stopped}

    @app.get("/processes")
    def processes():
        # Scoped to RoboTrader's own process/jobs, not a host-wide process
        # list — PID/memory/CPU of this engine, the scheduler's registered
        # jobs, and current backtest/sweep job status.
        return {
            **process_status(getattr(engine, "scheduler", None)),
            "mode": engine.settings.mode.value,
            "backtest_job": backtest_runner.status(),
            "sweep_job": sweep_runner.status(),
        }

    @app.get("/system/logs")
    def system_logs(limit: int = 200):
        return recent_logs(limit)

    @app.post("/system/logs/clear")
    def system_logs_clear():
        # Clears the in-memory stdout/stderr tail buffer only — NOT
        # journal/audit.sqlite, which is the system of record and has no
        # GUI-exposed delete path by design (README Rule 10).
        clear_log_buffer()
        return {"cleared": True}

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

    @app.post("/reconcile/run")
    def reconcile_run():
        # Manual on-demand version of the same reconcile_job() that already
        # runs at startup/09:00/reconnect — a safe operator control: it only
        # compares journal vs broker state and (on mismatch) engages the
        # existing RECONCILE halt, the same as the automatic path. It does
        # not size, place, or approve anything, so it doesn't cross into
        # risk-manager territory.
        report = engine.reconcile_job()
        return {"clean": report.clean, "mismatches": report.mismatches, "halt": engine.risk.halt}

    @app.post("/notes")
    def add_note(body: Note):
        # Free-text operator journal entry — folds into the same audited
        # trail as every other decision (README Rule 10), but carries no
        # authority of its own: it can't pause anything, resize anything,
        # or clear a halt. Pure record-keeping.
        engine.audit.event("operator_note", detail=body.note)
        return {"logged": True}

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
