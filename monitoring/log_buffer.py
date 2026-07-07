"""In-memory ring buffer of the engine's stdout/stderr — the GUI's
Processes tab live-tails this and can clear it.

Deliberately NOT the audit trail (journal/audit.sqlite): that's the system
of record (orders, fills, halts, tax lots) and the GUI has no way to touch
it. Under Docker, stdout goes to `docker compose logs`, which the process
itself can't truncate from inside the container — this buffer is the
in-app equivalent of a "clear log view" affordance, backed by nothing the
compliance/tax/Gate 2 path depends on.
"""
from __future__ import annotations

import sys
import threading
from collections import deque

_MAX_LINES = 500
_lines: deque[str] = deque(maxlen=_MAX_LINES)
_lock = threading.Lock()


class _Tee:
    def __init__(self, stream):
        self._stream = stream

    def write(self, data: str) -> int:
        n = self._stream.write(data)
        if data.strip():
            with _lock:
                for line in data.splitlines():
                    if line.strip():
                        _lines.append(line)
        return n

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install() -> None:
    """Idempotent — safe to call more than once (e.g. tests re-importing)."""
    if not isinstance(sys.stdout, _Tee):
        sys.stdout = _Tee(sys.stdout)
    if not isinstance(sys.stderr, _Tee):
        sys.stderr = _Tee(sys.stderr)


def recent(limit: int = 200) -> list[str]:
    with _lock:
        return list(_lines)[-limit:]


def clear() -> None:
    with _lock:
        _lines.clear()
