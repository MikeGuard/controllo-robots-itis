"""Repository model: projects as folders, iterations as commits, branches, nested files, exact-version runs."""
import asyncio, sqlite3, json
import pytest, database, workspace

MAIN = "from helper import home\nprint(home())\n"
HELPER = "def home():\n    return [0] * 6\n"


def _student_submit(client, files, project="proj", msg="", student="alice", model="ur"):
    r = client.post("/api/submit", json={"student_name": student, "program_name": project, "task_id": project,
                                          "project_name": project, "code": files["main.py"], "files": files,
                                          "entry_point": "main.py", "commit_message": msg or "x", "robot_model": model})
    assert r.status_code == 200, r.text
    return r.json()["submission_id"]


def test_legacy_iterations_become_commits_on_main(client, admin):
    ids = [_student_submit(client, {"main.py": f"print({i})\n"}, msg=f"it{i}") for i in range(3)]
    repos = client.get("/api/repos", headers=admin).json()["repos"]
    r = next(x for x in repos if x["project"] == "proj")
    assert r["commits"] == 3 and r["branches"] == ["main"] and r["head"]["id"] == ids[-1]
    commits = client.get("/api/repos/alice/proj/commits?branch=main", headers=admin).json()["commits"]
    assert [c["id"] for c in commits] == ids[::-1]                       # newest first
    assert commits[0]["author"] == "alice" and commits[0]["status"] == "pending"


def test_commit_detail_returns_all_files_single_and_multi(client, admin):
    single = _student_submit(client, {"main.py": "print(1)\n"}, project="single")
    multi = _student_submit(client, {"main.py": MAIN, "helper.py": HELPER}, project="multi")
    assert set(client.get(f"/api/repos/alice/single/commits/{single}", headers=admin).json()["files"]) == {"main.py"}
    assert set(client.get(f"/api/repos/alice/multi/commits/{multi}", headers=admin).json()["files"]) == {"main.py", "helper.py"}
    assert client.get(f"/api/repos/alice/single/commits/{multi}", headers=admin).status_code == 404   # wrong repo


def test_pre_migration_database_keeps_working(tmp_path, monkeypatch):
    """Old rows (no branch/parent/author columns) must appear as commits on main after init_db."""
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE submissions (id INTEGER PRIMARY KEY AUTOINCREMENT, student_name TEXT NOT NULL, task_id TEXT NOT NULL,
        code TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', logs TEXT DEFAULT '', submitted_at TEXT NOT NULL,
        executed_at TEXT, completed_at TEXT, robot_model TEXT NOT NULL DEFAULT 'ur')""")
    con.execute("INSERT INTO submissions (student_name, task_id, code, submitted_at) VALUES ('bob','old_prog','print(1)','2025-01-01T00:00:00')")
    con.commit(); con.close()
    monkeypatch.setattr(database, "DB_PATH", str(db))
    asyncio.run(database.init_db())
    commits = asyncio.run(database.list_commits("bob", "old_prog"))
    assert len(commits) == 1 and commits[0]["branch"] == "main" and commits[0]["files_count"] == 1
    assert asyncio.run(database.list_branches("bob", "old_prog"))[0]["name"] == "main"


def test_branch_commits_are_isolated_from_main_and_student(client, admin):
    base = _student_submit(client, {"main.py": "print('student')\n"})
    workspace.save_project("alice", "proj", {"main.py": "print('student')\n"})
    r = client.post("/api/repos/alice/proj/branches", headers=admin, json={"name": "fix-speed", "from_commit_id": base})
    assert r.status_code == 200
    c = client.post("/api/repos/alice/proj/commits", headers=admin, json={
        "branch": "fix-speed", "files": {"main.py": "print('branch')\n"}, "message": "tweak"}).json()
    assert c["synced_to_student"] is False and c["parent_id"] == base
    assert workspace.load_project("alice", "proj")["files"]["main.py"] == "print('student')\n"
    main = client.get("/api/repos/alice/proj/commits?branch=main", headers=admin).json()["commits"]
    br = client.get("/api/repos/alice/proj/commits?branch=fix-speed", headers=admin).json()["commits"]
    assert [x["id"] for x in main] == [base] and [x["id"] for x in br] == [c["commit_id"]]
    names = {b["name"]: b for b in client.get("/api/repos/alice/proj", headers=admin).json()["branches"]}
    assert set(names) == {"main", "fix-speed"} and names["fix-speed"]["head_id"] == c["commit_id"]


def test_new_branch_head_points_at_fork_commit(client, admin):
    base = _student_submit(client, {"main.py": "print(1)\n"})
    client.post("/api/repos/alice/proj/branches", headers=admin, json={"name": "b", "from_commit_id": base})
    b = {x["name"]: x for x in client.get("/api/repos/alice/proj", headers=admin).json()["branches"]}["b"]
    assert b["head_id"] == base and b["commits"] == 0


def test_commit_on_main_syncs_student_working_copy(client, admin):
    _student_submit(client, {"main.py": "print('old')\n"})
    workspace.save_project("alice", "proj", {"main.py": "print('old')\n"})
    c = client.post("/api/repos/alice/proj/commits", headers=admin, json={"files": {"main.py": "print('new')\n", "extra.py": "X=1\n"}, "message": "instructor"}).json()
    assert c["synced_to_student"] is True
    assert set(workspace.load_project("alice", "proj")["files"]) == {"main.py", "extra.py"}


def test_nested_files_commit_and_safe_imports(client, admin):
    files = {"main.py": "from lib.util import f\nprint(f())\n", "lib/__init__.py": "", "lib/util.py": "def f():\n    return 1\n", "data/cfg.json": "{}"}
    r = client.post("/api/repos", headers=admin, json={"project": "nested", "files": files})
    assert r.status_code == 200, r.text
    got = client.get(f"/api/repos/mike/nested/commits/{r.json()['commit_id']}", headers=admin).json()["files"]
    assert set(got) == set(files)


@pytest.mark.parametrize("bad", ["../evil.py", "/etc/x.py", "a/../../b.py", ".hidden.py", "x.exe"])
def test_bad_paths_rejected(client, admin, bad):
    r = client.post("/api/repos", headers=admin, json={"project": "p", "files": {"main.py": "print(1)\n", bad: "x"}})
    assert r.status_code == 400


def test_workspace_nested_roundtrip():
    workspace.save_project("u", "n", {"main.py": "1", "lib/a.py": "2", "lib/sub/b.py": "3"})
    assert workspace.load_project("u", "n")["files"] == {"main.py": "1", "lib/a.py": "2", "lib/sub/b.py": "3"}
    workspace.save_project("u", "n", {"main.py": "1"})
    assert workspace.load_project("u", "n")["files"] == {"main.py": "1"}


def test_run_sends_exactly_the_selected_commit(client, admin, monkeypatch):
    import app as app_module
    v1 = _student_submit(client, {"main.py": "print('v1')\n", "helper.py": HELPER}, msg="v1")
    v2 = _student_submit(client, {"main.py": "print('v2')\n"}, msg="v2")
    seen = {}
    async def fake_launch(sub_id, code, robot_model=None, files=None, entry_point="main.py", workspace_owner=None, workspace_project=None):
        seen.update(sub_id=sub_id, code=code, files=files, entry=entry_point, ws_owner=workspace_owner)
    monkeypatch.setattr(app_module, "launch_submission_execution", fake_launch)
    r = client.post(f"/api/submissions/{v1}/approve", headers=admin)       # run the OLD commit, not the head
    assert r.status_code == 200, r.text
    assert seen["sub_id"] == v1 and set(seen["files"]) == {"main.py", "helper.py"} and "v1" in seen["files"]["main.py"]


def test_branch_run_does_not_write_back_to_student_workspace(client, admin, monkeypatch):
    import app as app_module
    base = _student_submit(client, {"main.py": "print(1)\n"})
    client.post("/api/repos/alice/proj/branches", headers=admin, json={"name": "exp", "from_commit_id": base})
    c = client.post("/api/repos/alice/proj/commits", headers=admin, json={"branch": "exp", "files": {"main.py": "print(2)\n"}}).json()
    seen = {}
    async def fake_launch(sub_id, code, **kw): seen.update(kw)
    monkeypatch.setattr(app_module, "launch_submission_execution", fake_launch)
    assert client.post(f"/api/submissions/{c['commit_id']}/approve", headers=admin).status_code == 200
    assert seen["workspace_owner"] is None


def test_rename_and_delete_only_for_admin_owned(client, admin):
    _student_submit(client, {"main.py": "print(1)\n"})
    assert client.delete("/api/repos/alice/proj", headers=admin).status_code == 403
    assert client.post("/api/repos/alice/proj/rename", headers=admin, json={"new_name": "x"}).status_code == 403
    client.post("/api/repos", headers=admin, json={"project": "mine", "files": {"main.py": "print(1)\n"}})
    assert client.post("/api/repos/mike/mine/rename", headers=admin, json={"new_name": "mine2"}).status_code == 200
    assert client.delete("/api/repos/mike/mine2", headers=admin).status_code == 200
    assert client.get("/api/repos/mike/mine2", headers=admin).status_code == 404


def test_workspace_only_project_is_listed(client, admin):
    workspace.save_project("carol", "legacy", {"main.py": "print(1)\n"})
    r = next(x for x in client.get("/api/repos", headers=admin).json()["repos"] if x["project"] == "legacy")
    assert r["commits"] == 0 and r["has_working_copy"] is True
    d = client.get("/api/repos/carol/legacy", headers=admin).json()
    assert d["working_copy"]["files_count"] == 1
    assert client.get("/api/repos/carol/legacy/working-copy", headers=admin).json()["files"] == {"main.py": "print(1)\n"}


# ---------- student access ----------

@pytest.fixture()
def student(client):
    """A registered, approved student 'dora' with a session token."""
    client.post("/api/auth/register", json={"username": "dora", "password": "pw12345"})
    admin = client.post("/api/auth/login", json={"username": "mike", "password": "mike2088"}).json()["token"]
    users = client.get("/api/admin/users", headers={"Authorization": f"Bearer {admin}"}).json()
    uid = next(u["id"] for u in (users if isinstance(users, list) else users["users"]) if u["username"] == "dora")
    client.post(f"/api/admin/users/{uid}/approve", headers={"Authorization": f"Bearer {admin}"})
    tok = client.post("/api/auth/login", json={"username": "dora", "password": "pw12345"}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def test_student_sees_only_own_repos_and_history(client, student):
    a = _student_submit(client, {"main.py": "print(1)\n"}, project="mine", student="dora", msg="first")
    _student_submit(client, {"main.py": "print(2)\n"}, project="theirs", student="eve")
    repos = client.get("/api/repos", headers=student).json()["repos"]
    assert [r["project"] for r in repos] == ["mine"]
    assert client.get("/api/repos/dora/mine/commits", headers=student).json()["commits"][0]["id"] == a
    assert set(client.get(f"/api/repos/dora/mine/commits/{a}", headers=student).json()["files"]) == {"main.py"}


def test_student_cannot_touch_other_repos_or_admin_actions(client, student):
    other = _student_submit(client, {"main.py": "print(2)\n"}, project="theirs", student="eve")
    assert client.get("/api/repos/eve/theirs", headers=student).status_code == 403
    assert client.get(f"/api/repos/eve/theirs/commits/{other}", headers=student).status_code == 403
    assert client.post("/api/repos/eve/theirs/commits", headers=student, json={"files": {"main.py": "x=1\n"}}).status_code == 403
    assert client.post("/api/repos", headers=student, json={"project": "x", "files": {"main.py": "x=1\n"}}).status_code == 403
    assert client.delete("/api/repos/dora/anything", headers=student).status_code == 403
    assert client.post("/api/repos/dora/anything/rename", headers=student, json={"new_name": "y"}).status_code == 403


def test_student_branches_and_commits_stay_out_of_queue_history(client, admin, student):
    base = _student_submit(client, {"main.py": "print(1)\n"}, project="mine", student="dora")
    assert client.post("/api/repos/dora/mine/branches", headers=student, json={"name": "try-it", "from_commit_id": base}).status_code == 200
    c = client.post("/api/repos/dora/mine/commits", headers=student, json={"branch": "try-it", "files": {"main.py": "print(9)\n"}, "message": "idea"}).json()
    assert c["synced_to_student"] is False
    mine = client.get("/api/student/projects", headers=student).json()
    ids = [p["id"] for p in (mine if isinstance(mine, list) else mine.get("projects", []))]
    assert base in ids and c["commit_id"] not in ids          # branch commits are not "submissions" to the instructor
    assert client.get(f"/api/repos/dora/mine/commits/{c['commit_id']}", headers=admin).json()["author"] == "dora"
