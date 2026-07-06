"""Tax lot tracking for U.S. reporting.

FIFO lot matching over the journal's fill events: every buy opens a lot;
every sell closes lots oldest-first, producing realized rows with the Form
8949 column set (description, acquired, sold, proceeds, basis, gain,
short/long term). A realized loss followed by a re-buy of the same symbol
within 30 days is flagged wash=True — frequent re-entries in the same ETF
make these common, and the broker's 1099-B (which stays authoritative for
filing) will adjust them.
"""
from __future__ import annotations

import csv
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from journal.audit import AuditLog


@dataclass
class RealizedRow:
    symbol: str
    qty: float
    acquired: datetime
    sold: datetime
    proceeds: float
    basis: float
    gain: float
    long_term: bool
    wash: bool = False


def _ts(row: dict) -> datetime:
    raw = row.get("detail") or row["ts"]           # fill time preferred
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))


class TaxLedger:
    def __init__(self, audit: AuditLog | str):
        self.audit = audit if isinstance(audit, AuditLog) else AuditLog(audit)

    def realized(self, year: int | None = None) -> list[RealizedRow]:
        fills = sorted(self.audit.query("fill", limit=1_000_000), key=lambda r: r["id"])
        lots: dict[str, deque] = {}
        buys: dict[str, list[datetime]] = {}
        rows: list[RealizedRow] = []
        for f in fills:
            sym, qty, price, when = f["symbol"], float(f["qty"]), float(f["price"]), _ts(f)
            if f["side"] == "buy":
                lots.setdefault(sym, deque()).append([qty, price, when])
                buys.setdefault(sym, []).append(when)
                continue
            remaining = qty
            queue = lots.setdefault(sym, deque())
            while remaining > 1e-9 and queue:
                lot = queue[0]
                take = min(remaining, lot[0])
                basis, proceeds = take * lot[1], take * price
                rows.append(RealizedRow(
                    symbol=sym, qty=take, acquired=lot[2], sold=when,
                    proceeds=round(proceeds, 4), basis=round(basis, 4),
                    gain=round(proceeds - basis, 4),
                    long_term=(when - lot[2]) > timedelta(days=365),
                ))
                lot[0] -= take
                remaining -= take
                if lot[0] <= 1e-9:
                    queue.popleft()
        for r in rows:                              # wash-sale flags need all buys known
            if r.gain < 0:
                r.wash = any(r.sold < b <= r.sold + timedelta(days=30)
                             for b in buys.get(r.symbol, []))
        if year is not None:
            rows = [r for r in rows if r.sold.year == year]
        return rows

    def export_8949_csv(self, year: int, out_path: str) -> int:
        rows = self.realized(year)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["description", "date_acquired", "date_sold", "proceeds",
                        "cost_basis", "gain_loss", "term", "wash_sale_flag"])
            for r in rows:
                w.writerow([f"{r.qty:.6f} {r.symbol}", r.acquired.date(), r.sold.date(),
                            f"{r.proceeds:.2f}", f"{r.basis:.2f}", f"{r.gain:.2f}",
                            "long" if r.long_term else "short", "W" if r.wash else ""])
        return len(rows)
