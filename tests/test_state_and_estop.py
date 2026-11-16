import asyncio, database, pytest
import execution_engine
from execution_engine import engine

CODE = "print('hi')\n"


def _submit(client, name="p1", model="ur"):
    r = client.post("/api/submit", json={"student_name": "alice", "program_name": name, "task_id": name,
                                          "code": CODE, "robot_model": model})
    assert r.status_code == 200, r.text
    return r.json()["submission_id"]


def test_stale_running_row_is_closed_on_startup(tmp_path, monkeypatch):
    """Bug 2: a run interrupted by a restart stayed RUNNING forever."""
    from fastapi.testclient import TestClient
    import app as app_module
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "t.db"))
    with TestClient(app_module.app) as c:
        sid = _submit(c)
        asyncio.run(database.update_status(sid, "running"))
    with TestClient(app_module.app) as c:           # "restart"
        row = asyncio.run(database.get_submission(sid))
        assert row["status"] == "aborted"
        assert "interrupted" in row["logs"].lower()


def test_running_row_without_engine_run_is_reconciled_via_api(client, admin):
    sid = _submit(client)
    asyncio.run(database.update_status(sid, "running"))
    subs = client.get("/api/submissions", headers=admin).json()["submissions"]
    assert next(s for s in subs if s["id"] == sid)["status"] == "aborted"


def test_running_row_with_live_engine_run_is_kept(client, admin):
    sid = _submit(client)
    asyncio.run(database.update_status(sid, "running"))
    slot = engine.get_slot("ur"); slot.is_executing = True; slot.submission_id = sid
    try:
        subs = client.get("/api/submissions", headers=admin).json()["submissions"]
        assert next(s for s in subs if s["id"] == sid)["status"] == "running"
        st = client.get("/api/state", headers=admin).json()["robots"]["ur"]
        assert st["status"] == "running" and st["submission_id"] == sid
    finally:
        slot.is_executing = False; slot.submission_id = None


def test_state_reports_halted_and_offline(client, admin, monkeypatch):
    import app as app_module
    async def offline(ip=None, model=None, use_cache=True): return (False, False, False)
    monkeypatch.setattr(app_module, "check_robot_reachability", offline)
    assert client.get("/api/state", headers=admin).json()["robots"]["niryo"]["status"] == "offline"
    engine.get_slot("niryo").is_emergency_halted = True
    assert client.get("/api/state", headers=admin).json()["robots"]["niryo"]["status"] == "halted"


def test_run_is_refused_while_estop_latched_and_latch_survives(client, admin):
    """Launching used to silently clear the E-stop latch."""
    sid = _submit(client)
    engine.get_slot("ur").is_emergency_halted = True
    r = client.post(f"/api/submissions/{sid}/approve", headers=admin)
    assert r.status_code == 423
    assert engine.is_emergency_halted("ur") is True
    assert client.post("/api/emergency-reset?model=ur", headers=admin).status_code == 200
    assert engine.is_emergency_halted("ur") is False


def test_engine_refuses_when_latched():
    from execution_engine import EmergencyHaltedError
    slot = engine.get_slot("ur"); slot.is_emergency_halted = True
    try:
        with pytest.raises(EmergencyHaltedError):
            asyncio.run(engine.execute_submission(1, CODE, robot_model="ur"))
    finally:
        slot.is_emergency_halted = False


def test_setup_failure_releases_slot_and_closes_row(client, monkeypatch):
    """Failure before the worker starts used to leave the arm 'busy' and the row 'running' forever."""
    sid = _submit(client)
    def boom(*a, **k): raise OSError("disk full")
    monkeypatch.setattr(execution_engine.tempfile, "mkdtemp", boom)
    with pytest.raises(OSError):
        asyncio.run(engine.execute_submission(sid, CODE, robot_model="ur"))
    assert engine.is_model_executing("ur") is False
    assert asyncio.run(database.get_submission(sid))["status"] == "failed"


def test_abort_without_model_stops_every_robot(client, admin):
    r = client.post("/api/abort", headers=admin)
    assert r.status_code == 200
    assert engine.is_emergency_halted("ur") and engine.is_emergency_halted("niryo")
    client.post("/api/emergency-reset", headers=admin)
