"""Test harness: every test gets its own database and workspace folder, and robots are never contacted."""
import os, sys, tempfile
_TMP = tempfile.mkdtemp(prefix="ur_tests_")
os.environ["UR_WORKSPACES_DIR"] = os.path.join(_TMP, "workspaces")      # must be set before importing workspace
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import database, app as app_module
from execution_engine import engine


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.db"))

    async def fake_reach(ip=None, model=None, use_cache=True):   # never ping / connect to a real robot
        return (True, True, False)
    monkeypatch.setattr(app_module, "check_robot_reachability", fake_reach)
    for slot in engine.slots.values():
        slot.is_executing = slot.is_emergency_halted = slot.is_aborted = False
        slot.submission_id = None
        slot.process = None
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture()
def admin(client):
    r = client.post("/api/auth/login", json={"username": "mike", "password": "mike2088"})   # seeded superadmin
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}
