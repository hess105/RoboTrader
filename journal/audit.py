"""Append-only audit log in SQLite. Every signal, risk decision, order state
change, fill, halt, reconcile result, and operator action gets a row with a
UTC timestamp. Events are never updated or deleted; corrections are new rows.
The schema has fixed, typed columns — no free-form config/dict column — so
secrets are structurally unloggable here.

The orders table is the idempotency ledger for execution/order_manager.py:
an order row exists (status pending_submit) BEFORE the first submit attempt,
and recovery resolves those rows against the broker by client_order_id.

Also the persistence layer the risk manager uses to survive restarts:
peak equity is derived from journaled equity marks, so restarting the engine
can never reset a drawdown.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    kind     TEXT NOT NULL,
    symbol   TEXT,
    side     TEXT,
    qty      REAL,
    price    REAL,
    order_id TEXT,
    reason   TEXT,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    broker_order_id TEXT,
    ts       TEXT,
    symbol   TEXT,
    side     TEXT,
    notional REAL,
    qty      REAL,
    status   TEXT
);
"""

_ORDER_COLS = ("client_order_id", "broker_order_id", "ts", "symbol", "side",
               "notional", "qty", "status")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditLog:
    def __init__(self, db_path: str = "journal/audit.sqlite"):
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._c = sqlite3.connect(db_path, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._c.executescript(_SCHEMA)

    def event(self, kind: str, *, symbol: str | None = None, side: str | None = None,
              qty: float | None = None, price: float | None = None,
              order_id: str | None = None, reason: str | None = None,
              detail: str | None = None) -> None:
        with self._c:
            self._c.execute(
                "INSERT INTO events (ts, kind, symbol, side, qty, price, order_id, reason, detail)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (_utcnow(), kind, symbol, side, qty, price, order_id, reason, detail),
            )

    def query(self, kind: str | None = None, limit: int = 500) -> list[dict]:
        sql, args = "SELECT * FROM events", []
        if kind:
            sql += " WHERE kind = ?"
            args.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._c.execute(sql, args)]

    # --- orders (idempotency ledger) ---

    def upsert_order(self, client_order_id: str, **fields) -> None:
        row = self._c.execute(
            "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        merged = dict(row) if row else dict.fromkeys(_ORDER_COLS)
        merged["client_order_id"] = client_order_id
        merged.setdefault("ts", None)
        merged["ts"] = merged["ts"] or _utcnow()
        merged.update({k: v for k, v in fields.items() if v is not None})
        with self._c:
            self._c.execute(
                f"REPLACE INTO orders ({','.join(_ORDER_COLS)}) VALUES ({','.join('?' * len(_ORDER_COLS))})",
                tuple(merged[c] for c in _ORDER_COLS),
            )

    def pending_orders(self) -> list[dict]:
        return [dict(r) for r in self._c.execute(
            "SELECT * FROM orders WHERE status = 'pending_submit'")]

    def orders_by_status(self, status: str) -> list[dict]:
        return [dict(r) for r in self._c.execute(
            "SELECT * FROM orders WHERE status = ?", (status,))]

    def orders_recent(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self._c.execute(
            "SELECT * FROM orders ORDER BY ts DESC LIMIT ?", (limit,))]

    def latest_stop(self, symbol: str) -> dict | None:
        """Most recent engine-recorded protective stop for a symbol (reason
        carries the owning strategy's name)."""
        row = self._c.execute(
            "SELECT price, ts, reason FROM events WHERE kind = 'stop_set' AND symbol = ?"
            " ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
        return dict(row) if row else None

    def net_positions(self) -> dict[str, float]:
        """Journal-expected net position per symbol, derived from fills.
        The reconciler compares this against what the broker reports."""
        rows = self._c.execute(
            "SELECT symbol, SUM(CASE WHEN side = 'buy' THEN qty ELSE -qty END) AS net"
            " FROM events WHERE kind = 'fill' GROUP BY symbol")
        return {r["symbol"]: r["net"] for r in rows if abs(r["net"] or 0) > 1e-9}

    # --- risk persistence ---

    def daily_equity(self, days: int) -> list[tuple[str, float]]:
        """Last equity mark per calendar day, oldest first — restores the
        risk manager's vol-targeting window across restarts."""
        rows = self._c.execute(
            "SELECT substr(detail, 1, 10) AS day, price FROM events"
            " WHERE kind = 'equity_mark' GROUP BY day"
            " HAVING id = MAX(id) ORDER BY day DESC LIMIT ?", (days,)).fetchall()
        return [(r["day"], r["price"]) for r in reversed(rows)]

    def max_equity(self) -> float | None:
        """Peak equity for drawdown tracking. A peak_rebase event (written on
        manual reset after a drawdown halt) floors the peak at the rebased
        value and ignores older, higher marks."""
        rebase = self._c.execute(
            "SELECT id, price FROM events WHERE kind = 'peak_rebase'"
            " ORDER BY id DESC LIMIT 1").fetchone()
        if rebase is not None:
            row = self._c.execute(
                "SELECT MAX(price) FROM events WHERE kind = 'equity_mark' AND id > ?",
                (rebase["id"],)).fetchone()
            return max(rebase["price"], row[0]) if row[0] is not None else rebase["price"]
        row = self._c.execute(
            "SELECT MAX(price) FROM events WHERE kind = 'equity_mark'").fetchone()
        return row[0]
