"""Control API: read endpoints serve, command endpoints enforce their guards
(FLATTEN token, required post-mortem note), and the GUI can never bypass the
engine — every command routes through engine methods.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from journal.audit import AuditLog
from risk.manager import RiskManager
from service.api import create_app
from tests.test_risk_manager import make_cfg


class StubKillSwitch:
    def __init__(self):
        self.fired = None

    def fire(self, source, reason, **kwargs):
        self.fired = (source, reason)
        return True


class StubEngine:
    def __init__(self):
        self.audit = AuditLog(":memory:")
        self.risk = RiskManager(make_cfg(), self.audit)
        self.kill_switch = StubKillSwitch()
        self.paused = False

    def status(self):
        return {"mode": "paper", "halt": self.risk.halt, "paused": self.paused}

    def account_summary(self):
        return {"equity": 500.0}

    def positions_view(self):
        return []

    def orders_view(self, limit=200):
        return self.audit.orders_recent(limit)

    def set_paused(self, paused, note):
        self.paused = paused


def make_client():
    engine = StubEngine()
    return engine, TestClient(create_app(engine))


def test_read_endpoints_serve():
    _, client = make_client()
    assert client.get("/status").json()["mode"] == "paper"
    assert client.get("/account").json()["equity"] == 500.0
    assert client.get("/positions").json() == []
    assert client.get("/orders").json() == []
    assert "limits" in client.get("/risk").json()
    assert isinstance(client.get("/logs").json(), list)
    assert isinstance(client.get("/backtests").json(), list)


def test_killswitch_requires_exact_token():
    engine, client = make_client()
    r = client.post("/killswitch", json={"confirm": "yes please"})
    assert r.status_code == 400
    assert engine.kill_switch.fired is None
    r = client.post("/killswitch", json={"confirm": "FLATTEN", "reason": "drill"})
    assert r.status_code == 200 and r.json()["flat"] is True
    assert engine.kill_switch.fired == ("api", "drill")


def test_halt_reset_requires_note():
    _, client = make_client()
    assert client.post("/halt/reset", json={"note": "  "}).status_code == 400
    assert client.post("/halt/reset", json={"note": "reviewed"}).status_code == 200


def test_pause_resume_roundtrip():
    engine, client = make_client()
    client.post("/strategy/pause", json={"note": "x"})
    assert engine.paused is True
    client.post("/strategy/resume", json={"note": "x"})
    assert engine.paused is False
