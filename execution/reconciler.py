"""Local-state vs broker-state reconciliation.

Compares the journal's fill-derived expectation of holdings (audit
net_positions) against what the broker actually reports. Runs blocking at
every engine startup, daily at 09:00 ET, and after reconnects. The broker is
always the source of truth for what we HOLD; the journal is the source of
truth for WHY — a mismatch means the journal missed something (engine down
during a fill, manual trading in the same account) and a human should look
before the engine acts on stale beliefs. On mismatch the engine engages
HaltState.RECONCILE (entries blocked, exits allowed, stops still monitored).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from journal.audit import AuditLog


@dataclass
class ReconcileReport:
    clean: bool
    mismatches: list[str] = field(default_factory=list)


class Reconciler:
    def __init__(self, client, audit: AuditLog, alerts=None):
        self.client = client
        self.audit = audit
        self.alerts = alerts

    def run(self) -> ReconcileReport:
        expected = self.audit.net_positions()
        broker = {p.symbol: float(p.qty) for p in self.client.positions()}
        mismatches = []
        for sym in sorted(set(expected) | set(broker)):
            exp, got = expected.get(sym, 0.0), broker.get(sym, 0.0)
            if abs(exp - got) > 1e-6:
                mismatches.append(f"{sym}: journal expects {exp:.6f}, broker reports {got:.6f}")
        clean = not mismatches
        self.audit.event("reconcile",
                         reason="clean" if clean else "; ".join(mismatches))
        if not clean and self.alerts is not None:
            self.alerts.send("CRIT", "reconcile",
                             f"{len(mismatches)} mismatch(es): {'; '.join(mismatches)}")
        return ReconcileReport(clean, mismatches)
