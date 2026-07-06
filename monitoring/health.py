"""Connection health, fed by the engine's minute tick (each tick makes real
broker calls, so a tick failure IS a connectivity failure — no separate probe).

Escalation with positions on (failures ~= minutes of outage):
  - disconnect_alert_min consecutive failures -> CRIT alert
  - kill_after_min consecutive failures -> arm kill-on-reconnect. Protective
    stops are engine-monitored (Alpaca doesn't support stop orders on
    fractional shares), so a prolonged outage means unprotected positions;
    policy is to flatten as soon as connectivity returns. Set
    monitoring.kill_after_disconnect_min: 0 to disable.
The engine calls record_success() each healthy tick; a True return means the
armed kill switch should fire now.
"""
from __future__ import annotations


class HealthMonitor:
    def __init__(self, alerts=None, disconnect_alert_min: int = 5,
                 kill_after_min: int = 15):
        self.alerts = alerts
        self.disconnect_alert_min = disconnect_alert_min
        self.kill_after_min = kill_after_min
        self.failures = 0
        self.kill_armed = False
        self.ok = True
        self.last_error: str | None = None

    def record_failure(self, has_positions: bool, error: str = "") -> None:
        self.failures += 1
        self.ok = False
        self.last_error = error
        if self.failures == self.disconnect_alert_min:
            self._alert("CRIT", f"broker unreachable for ~{self.failures} min: {error}")
        if (has_positions and self.kill_after_min
                and self.failures >= self.kill_after_min and not self.kill_armed):
            self.kill_armed = True
            self._alert("CRIT", "kill-on-reconnect ARMED: prolonged outage with "
                                "positions on and engine-side stops blind")

    def record_success(self) -> bool:
        """Returns True if an armed kill switch should fire now."""
        fire = self.kill_armed
        if self.failures:
            self._alert("INFO", f"broker connectivity restored after {self.failures} failed ticks")
        self.failures = 0
        self.ok = True
        self.kill_armed = False
        return fire

    def status(self) -> dict:
        return {"ok": self.ok, "consecutive_failures": self.failures,
                "kill_armed": self.kill_armed, "last_error": self.last_error}

    def _alert(self, severity: str, message: str) -> None:
        if self.alerts is not None:
            self.alerts.send(severity, "health", message)
