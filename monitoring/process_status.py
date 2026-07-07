"""RoboTrader's own process status for the GUI's Processes tab — PID,
uptime, memory/CPU, and the scheduler's registered jobs. Deliberately
scoped to this software, not a host-wide process list.
"""
from __future__ import annotations

import os
import time

import psutil

_PROC = psutil.Process(os.getpid())


def process_status(scheduler=None) -> dict:
    with _PROC.oneshot():
        mem_mb = round(_PROC.memory_info().rss / (1024 * 1024), 1)
        # interval=None: non-blocking, compares against the previous call
        # (first call returns 0.0) — a blocking interval would stall this
        # GET request, which is polled every few seconds by the GUI.
        cpu_pct = _PROC.cpu_percent(interval=None)
        threads = _PROC.num_threads()
        uptime_sec = time.time() - _PROC.create_time()

    jobs = []
    if scheduler is not None:
        for job in scheduler.get_jobs():
            next_run = getattr(job, "next_run_time", None)
            jobs.append({"id": job.id, "name": job.name,
                        "next_run": next_run.isoformat() if next_run else None})

    return {
        "pid": os.getpid(),
        "uptime_sec": round(uptime_sec, 1),
        "memory_mb": mem_mb,
        "cpu_percent": cpu_pct,
        "threads": threads,
        "scheduler_jobs": jobs,
    }
