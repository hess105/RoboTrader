"""Kill switch. Lives in the ENGINE process; the GUI button and the CLI
(scripts/kill.py) are both just clients of the same engine endpoint, so a
frozen or crashed GUI can never disable it.

Sequence (each step journaled, alerts at start and end):
  1. HaltState.KILL_SWITCH engaged (blocks all new intents immediately)
  2. every open order cancelled
  3. every position market-flattened via close_all_positions()
  4. poll until the broker reports flat; CRIT alert if not flat in time
  5. remain halted until manual_reset with a journaled reason
"""
from __future__ import annotations

import time

from journal.audit import AuditLog
from risk.manager import HaltState, RiskManager


class KillSwitch:
    def __init__(self, client, risk: RiskManager, audit: AuditLog, alerts=None):
        self.client = client
        self.risk = risk
        self.audit = audit
        self.alerts = alerts

    def fire(self, source: str, reason: str,
             poll_timeout_sec: float = 60.0, poll_interval_sec: float = 2.0) -> bool:
        self.audit.event("kill_switch", reason=f"{source}: {reason}")
        self._alert("CRIT", f"KILL SWITCH FIRED ({source}): {reason} — flattening")
        self.risk.engage(HaltState.KILL_SWITCH, f"kill switch ({source}): {reason}")

        for order in self.client.open_orders():
            try:
                self.client.cancel(order.client_order_id)
            except Exception as exc:                  # noqa: BLE001 — keep flattening
                self.audit.event("kill_switch_error", order_id=order.client_order_id,
                                 reason=f"cancel failed: {exc}")
        try:
            self.client.close_all_positions()
        except Exception as exc:                      # noqa: BLE001
            self.audit.event("kill_switch_error", reason=f"close_all failed: {exc}")

        deadline = time.monotonic() + poll_timeout_sec
        flat = not self.client.positions()
        while not flat and time.monotonic() < deadline:
            time.sleep(poll_interval_sec)
            flat = not self.client.positions()

        if flat:
            self.audit.event("kill_switch_done", reason="flat confirmed")
            self._alert("CRIT", "kill switch complete: account flat, trading halted")
        else:
            self.audit.event("kill_switch_error", reason="NOT flat after timeout")
            self._alert("CRIT", "kill switch INCOMPLETE: positions remain — intervene manually")
        return flat

    def _alert(self, severity: str, message: str) -> None:
        if self.alerts is not None:
            self.alerts.send(severity, "kill_switch", message)
