"""Background job runner for the GUI's "Run Backtest" button.

One job at a time — this is a personal research tool, not a queue service.
Runs in a plain thread (not asyncio) because data fetch + the backtest loop
are both synchronous/blocking; a thread keeps the API's event loop (and
/status polling) responsive while it runs. It's still real CPU/GIL
contention on a 1-vCPU droplet, which is exactly why service/api.py refuses
to start one while the engine is in LIVE mode — see create_app().
"""
from __future__ import annotations

import threading
import time

from backtest.runner import run_backtest


class BacktestJobRunner:
    def __init__(self):
        self._lock = threading.Lock()
        self._state: dict = {"status": "idle"}

    def is_running(self) -> bool:
        return self._state.get("status") == "running"

    def status(self) -> dict:
        return dict(self._state)

    def start(self, start: str | None, end: str | None) -> None:
        with self._lock:
            if self.is_running():
                raise RuntimeError("A backtest is already running")
            self._state = {"status": "running", "message": "starting…",
                            "started_at": time.time()}

        def progress(msg: str) -> None:
            self._state = {**self._state, "message": msg}

        def worker() -> None:
            try:
                out = run_backtest(start, end, on_progress=progress)
                self._state = {"status": "done", "run_id": out["run_id"],
                               "metrics": out["metrics"], "gate1": out["gate1"]}
            except RuntimeError as exc:
                self._state = {"status": "error", "error": str(exc)}
            except Exception as exc:                      # noqa: BLE001 — surface, never crash the engine
                self._state = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

        threading.Thread(target=worker, daemon=True).start()


runner = BacktestJobRunner()
