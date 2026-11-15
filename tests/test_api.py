from fastapi.testclient import TestClient

from app.api.main import app

c = TestClient(app)
H = {"Authorization": "Bearer change-me"}


def test_health_and_status():
    assert c.get("/health/live").json() == {"status": "alive"}
    st = c.get("/api/status").json()
    assert st["database"]["status"] == "up"
    assert {f["instrument"] for f in st["market_data"]} == {"GOLD", "NASDAQ"}
    # no services running in the test -> not ready
    assert c.get("/health/ready").status_code == 503
    assert "strategylab_database_up 1" in c.get("/metrics").text
    assert c.get("/").status_code == 200


def test_strategy_crud_requires_token():
    body = {"name": "x", "kind": "sma_cross", "instrument": "GOLD", "timeframe": "5m"}
    assert c.post("/api/strategies", json=body).status_code == 401
    r = c.post("/api/strategies", json=body, headers=H)
    assert r.status_code == 201
    sid = r.json()["id"]
    assert c.patch(f"/api/strategies/{sid}", json={"paper_active": True}, headers=H).json()["paper_active"]
    assert c.post("/api/strategies", json={**body, "instrument": "BTC"}, headers=H).status_code == 400


def test_jobs_and_schedules():
    assert c.post("/api/jobs", json={"kind": "nope"}, headers=H).status_code == 400
    jid = c.post("/api/jobs", json={"kind": "compare"}, headers=H).json()["id"]
    assert c.get(f"/api/jobs/{jid}").json()["status"] == "queued"
    r = c.post("/api/schedules", json={"name": "ny", "kind": "compare", "cron": "0 9 * * 1-5",
                                       "timezone": "America/New_York"}, headers=H)
    assert r.status_code == 201 and r.json()["next_run_at"]
