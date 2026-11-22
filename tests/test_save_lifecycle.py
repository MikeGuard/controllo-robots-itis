"""Save / commit lifecycle of a student project, end to end over the HTTP API (what the student page calls)."""
import os
import pytest
import workspace


@pytest.fixture()
def student(client, admin):
    client.post("/api/auth/register", json={"username": "newbie", "password": "pw12345"})
    users = client.get("/api/admin/users", headers=admin).json()
    uid = next(u["id"] for u in (users if isinstance(users, list) else users["users"]) if u["username"] == "newbie")
    client.post(f"/api/admin/users/{uid}/approve", headers=admin)
    tok = client.post("/api/auth/login", json={"username": "newbie", "password": "pw12345"}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


MAIN = "print('main')\n"


def _save(client, h, files, project="lab", entry="main.py"):
    return client.put(f"/api/workspace/projects/{project}", headers=h, json={"files": files, "entry_point": entry, "robot_model": "ur"})


def test_reported_flow_deleting_a_text_file_never_touches_main(client, student):
    """New user -> project -> branch -> simulate -> write a .txt that sorts after main.py -> delete it."""
    assert client.post("/api/workspace/projects", headers=student, json={"name": "lab", "files": {"main.py": MAIN}}).status_code == 200
    assert client.post("/api/repos/newbie/lab/branches", headers=student, json={"name": "try", "from_commit_id": None}).status_code == 200
    sim = client.post("/api/simulate", json={"code": MAIN, "files": {"main.py": MAIN}, "entry_point": "main.py", "robot_model": "ur"})
    assert sim.status_code == 200 and sim.json()["success"], sim.text
    for txt in ("zz_notes.txt", "notes.txt", "a_first.txt"):
        assert _save(client, student, {"main.py": MAIN, txt: "text that must stay in its own file"}).status_code == 200
        assert _save(client, student, {"main.py": MAIN}).status_code == 200          # the file deleted in the editor
        wc = client.get("/api/repos/newbie/lab/working-copy", headers=student).json()
        assert wc["files"] == {"main.py": MAIN}, txt


def test_branch_commit_leaves_main_working_copy_alone_and_main_commit_updates_it(client, student):
    client.post("/api/workspace/projects", headers=student, json={"name": "lab", "files": {"main.py": MAIN}})
    first = client.post("/api/repos/newbie/lab/commits", headers=student, json={"branch": "main", "files": {"main.py": MAIN}, "message": "v1"}).json()
    client.post("/api/repos/newbie/lab/branches", headers=student, json={"name": "exp", "from_commit_id": first["commit_id"]})
    c = client.post("/api/repos/newbie/lab/commits", headers=student, json={
        "branch": "exp", "parent_id": first["commit_id"], "files": {"main.py": "print('exp')\n", "data.txt": "x"}, "message": "try"}).json()
    assert c["parent_id"] == first["commit_id"]
    assert workspace.load_project("newbie", "lab")["files"] == {"main.py": MAIN}          # branch never reaches main's working copy
    c2 = client.post("/api/repos/newbie/lab/commits", headers=student, json={"branch": "main", "files": {"main.py": "print('v2')\n"}, "message": "v2"}).json()
    assert c2["synced_to_student"] is True
    assert workspace.load_project("newbie", "lab")["files"] == {"main.py": "print('v2')\n"}


def test_rename_keeps_commit_history_attached(client, student):
    client.post("/api/workspace/projects", headers=student, json={"name": "lab", "files": {"main.py": MAIN}})
    cid = client.post("/api/repos/newbie/lab/commits", headers=student, json={"files": {"main.py": MAIN}, "message": "v1"}).json()["commit_id"]
    client.post("/api/repos/newbie/lab/branches", headers=student, json={"name": "exp", "from_commit_id": cid})
    r = client.post("/api/workspace/projects/lab/rename", headers=student, json={"new_name": "lab_final"})
    assert r.status_code == 200, r.text
    commits = client.get("/api/repos/newbie/lab_final/commits", headers=student).json()["commits"]
    assert [c["id"] for c in commits] == [cid]
    assert {b["name"] for b in client.get("/api/repos/newbie/lab_final", headers=student).json()["branches"]} == {"main", "exp"}
    assert client.get("/api/repos/newbie/lab/commits", headers=student).json()["commits"] == []


def test_rename_onto_a_name_with_history_is_refused(client, student):
    client.post("/api/workspace/projects", headers=student, json={"name": "a", "files": {"main.py": MAIN}})
    client.post("/api/repos/newbie/b/commits", headers=student, json={"files": {"main.py": MAIN}, "message": "old"})   # history under "b"
    assert client.post("/api/workspace/projects/a/rename", headers=student, json={"new_name": "b"}).status_code == 409
    assert workspace.project_exists("newbie", "a")


def test_failed_project_creation_leaves_no_empty_folder(client, student):
    r = client.post("/api/workspace/projects", headers=student, json={"name": "bad", "files": {"main.py": MAIN, "x.exe": "?"}})
    assert r.status_code == 400
    assert not workspace.project_exists("newbie", "bad")


def test_interrupted_save_leftovers_never_break_later_saves(client, student):
    client.post("/api/workspace/projects", headers=student, json={"name": "lab", "files": {"main.py": MAIN}})
    pdir = os.path.join(workspace.WORKSPACES_DIR, "newbie", "lab")
    open(os.path.join(pdir, "main.py.tmp"), "w").write("half written")                 # old-style temp file left by a crash
    assert set(client.get("/api/workspace/projects/lab", headers=student).json()["files"]) == {"main.py"}
    assert _save(client, student, {"main.py": "print(2)\n"}).status_code == 200
    assert sorted(os.listdir(pdir)) == [".meta.json", "main.py"]                          # leftover cleaned, temp files hidden


def test_entry_point_survives_save_and_reload(client, student):
    client.post("/api/workspace/projects", headers=student, json={"name": "lab", "files": {"main.py": MAIN}})
    _save(client, student, {"main.py": MAIN, "run.py": "print('run')\n"}, entry="run.py")
    assert client.get("/api/workspace/projects/lab", headers=student).json()["entry_point"] == "run.py"
    _save(client, student, {"notes.txt": "x", "run2.py": "print(1)\n"}, entry="gone.py")    # entry deleted: a .py is picked, not the .txt
    assert client.get("/api/workspace/projects/lab", headers=student).json()["entry_point"] == "run2.py"


def test_one_validator_for_every_entry_point(client, admin):
    bad = "import os\nos.system('rm -rf /')\n"
    files = {"main.py": "import helper\n", "helper.py": bad}
    chk = client.post("/api/check-syntax", json={"code": "", "files": files}).json()
    assert chk["valid"] is False and all(e.startswith("[helper.py]") for e in chk["errors"])
    assert client.post("/api/simulate", json={"code": "", "files": files}).status_code == 422
    assert client.post("/api/repos/mike/p/commits", headers=admin, json={"files": files}).status_code in (404, 422)
    r = client.post("/api/admin/send-code", headers=admin, json={"code": bad, "files": {"main.py": "print(1)\n"}})
    assert r.status_code == 422                     # the entry code is checked even when files are sent
    assert workspace.resolve_entry_point({"a.txt": "", "b.py": ""}) == "b.py"
    assert workspace.detect_robot_model("from pyniryo import NiryoRobot") == "niryo"
