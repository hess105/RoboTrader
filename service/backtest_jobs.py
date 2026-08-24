"""Background job runner for the GUI's "Run Backtest" button.

One job at a time — this is a personal research tool, not a queue service.
Runs in a spawned SUBPROCESS, not a thread: run_backtest() has no natural
checkpoints to cooperatively check a "please stop" flag (it's a single
blocking data-fetch call followed by a single blocking backtest-engine
call), so the only reliable way to honor a "Stop" button is an OS-level
process kill via terminate(). A thread can't be forcibly killed in Python;
a process can.
"""
from __future__ import annotations

import multiprocessing as mp
import queue
import threading
import time

from backtest.runner import run_backtest


def _subprocess_entry(start, end, label, capital, result_q, progress_q) -> None:
    def progress(msg: str) -> None:
        try:
            progress_q.put_nowait(msg)
        except Exception:                                 # noqa: BLE001
            pass

    try:
        out = run_backtest(start, end, on_progress=progress, label=label, capital=capital)
        result_q.put(("done", out))
    except RuntimeError as exc:
        result_q.put(("error", str(exc)))
    except Exception as exc:                               # noqa: BLE001 — surface, never crash the engine
        result_q.put(("error", f"{type(exc).__name__}: {exc}"))


class BacktestJobRunner:
    def __init__(self):
        self._lock = threading.Lock()
        self._state: dict = {"status": "idle"}
        self._process: mp.process.BaseProcess | None = None

    def is_running(self) -> bool:
        return self._state.get("status") == "running"

    def status(self) -> dict:
        return dict(self._state)

    def start(self, start: str | None, end: str | None, label: str | None = None,
              capital: float | None = None) -> None:
        with self._lock:
            if self.is_running():
                raise RuntimeError("A backtest is already running")
            self._state = {"status": "running", "message": "starting…",
                            "started_at": time.time()}

        ctx = mp.get_context("spawn")
        result_q: mp.Queue = ctx.Queue()
        progress_q: mp.Queue = ctx.Queue()
        proc = ctx.Process(target=_subprocess_entry,
                            args=(start, end, label, capital, result_q, progress_q), daemon=True)
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
            try:
                # A short timeout, not result_q.empty(): put() in the child
                # hands off to a background feeder thread that flushes
                # asynchronously, so a fast failure (dies before any real
                # work — e.g. a bad request built before the first fetch)
                # can have the child process fully exit before that thread
                # finishes writing to the pipe. Checking empty() right after
                # join() can then wrongly see "nothing there" and report
                # this as a silent "stopped" instead of the real error.
                kind, payload = result_q.get(timeout=5.0)
                if kind == "done":
                    self._state = {"status": "done", **payload}
                else:
                    self._state = {"status": "error", "error": payload}
            except queue.Empty:
                # Genuinely nothing to report — e.g. stop() called
                # terminate() before the child ever reached a put().
                self._state = {"status": "stopped"}
            self._process = None

        threading.Thread(target=monitor, daemon=True).start()

    def stop(self) -> bool:
        if not self.is_running() or self._process is None:
            return False
        self._process.terminate()
        return True


runner = BacktestJobRunner()
