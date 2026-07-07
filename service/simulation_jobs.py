"""Background job runner for the GUI's "Run Simulated Day" button. Mirrors
service/backtest_jobs.py's subprocess+queue pattern exactly (see that file
for why a subprocess, not a thread) — one job at a time, personal tool.
"""
from __future__ import annotations

import multiprocessing as mp
import queue
import threading
import time

from service.simulate_day import run_simulated_day


def _subprocess_entry(result_q, progress_q) -> None:
    def progress(msg: str) -> None:
        try:
            progress_q.put_nowait(msg)
        except Exception:                                 # noqa: BLE001
            pass

    try:
        out = run_simulated_day(on_progress=progress)
        result_q.put(("done", out))
    except Exception as exc:                               # noqa: BLE001 — surface, never crash the engine
        result_q.put(("error", f"{type(exc).__name__}: {exc}"))


class SimulationJobRunner:
    def __init__(self):
        self._lock = threading.Lock()
        self._state: dict = {"status": "idle"}
        self._process: mp.process.BaseProcess | None = None

    def is_running(self) -> bool:
        return self._state.get("status") == "running"

    def status(self) -> dict:
        return dict(self._state)

    def start(self) -> None:
        with self._lock:
            if self.is_running():
                raise RuntimeError("A simulation is already running")
            self._state = {"status": "running", "message": "starting…",
                            "started_at": time.time()}

        ctx = mp.get_context("spawn")
        result_q: mp.Queue = ctx.Queue()
        progress_q: mp.Queue = ctx.Queue()
        proc = ctx.Process(target=_subprocess_entry, args=(result_q, progress_q), daemon=True)
        self._process = proc
        proc.start()

        def monitor() -> None:
            while proc.is_alive():
                try:
                    msg = progress_q.get(timeout=0.3)
                    self._state = {**self._state, "message": msg}
                except queue.Empty:
                    pass
            proc.join()
            if not result_q.empty():
                kind, payload = result_q.get()
                if kind == "done":
                    self._state = {"status": "done", **payload}
                else:
                    self._state = {"status": "error", "error": payload}
            else:
                self._state = {"status": "stopped"}
            self._process = None

        threading.Thread(target=monitor, daemon=True).start()

    def stop(self) -> bool:
        if not self.is_running() or self._process is None:
            return False
        self._process.terminate()
        return True


runner = SimulationJobRunner()
