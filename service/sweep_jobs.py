"""Background job runner for the GUI's "Run Sweep" button — same pattern as
service/backtest_jobs.py applied to the joint parameter sweep. One job at a
time; service/api.py also cross-checks against backtest_jobs so a sweep and
a single backtest can't run concurrently and fight over the same CPU core.
"""
from __future__ import annotations

import threading
import time

from backtest.sweep import run_sweep


class SweepJobRunner:
    def __init__(self):
        self._lock = threading.Lock()
        self._state: dict = {"status": "idle"}

    def is_running(self) -> bool:
        return self._state.get("status") == "running"

    def status(self) -> dict:
        return dict(self._state)

    def start(self, n_samples: int, workers: int, is_end: str,
              oos_start: str, seed: int) -> None:
        with self._lock:
            if self.is_running():
                raise RuntimeError("A sweep is already running")
            self._state = {"status": "running", "message": "starting…",
                            "started_at": time.time()}

        def progress(msg: str) -> None:
            self._state = {**self._state, "message": msg}

        def worker() -> None:
            try:
                out = run_sweep(n_samples, workers, is_end, oos_start, seed,
                                 on_progress=progress)
                self._state = {"status": "done", **out}
            except RuntimeError as exc:
                self._state = {"status": "error", "error": str(exc)}
            except Exception as exc:                      # noqa: BLE001 — surface, never crash the engine
                self._state = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

        threading.Thread(target=worker, daemon=True).start()


runner = SweepJobRunner()
