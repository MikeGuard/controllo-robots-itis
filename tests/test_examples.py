MAIN = "from helper import home\nprint(home())\n"
HELPER = "def home():\n    return [0, 0, 0, 0, 0, 0]\n"


def test_multi_file_example_roundtrip(client, admin):
    """Bug 7: only one file was saved."""
    hist = [{"commit_message": "v1", "files": {"main.py": "print(1)\n"}, "entry_point": "main.py"}]
    r = client.post("/api/examples", headers=admin, json={
        "title": "multi", "robot_model": "ur", "files": {"main.py": MAIN, "helper.py": HELPER, "data.json": "{}"},
        "entry_point": "main.py", "history": hist})
    assert r.status_code == 200, r.text
    ex = client.get(f"/api/examples/{r.json()['example_id']}").json()
    assert set(ex["files"]) == {"main.py", "helper.py", "data.json"}
    assert ex["entry_point"] == "main.py" and ex["history"] == hist


def test_legacy_single_file_example_still_loads(client, admin):
    r = client.post("/api/examples", headers=admin, json={"title": "old", "code": "print(1)\n"})
    ex = client.get(f"/api/examples/{r.json()['example_id']}").json()
    assert ex["files"] == {"main.py": "print(1)\n"}


def test_example_with_local_import_passes_validation(client, admin):
    assert client.post("/api/examples", headers=admin, json={
        "title": "imports", "files": {"main.py": MAIN, "helper.py": HELPER}}).status_code == 200


def test_example_update_keeps_all_files(client, admin):
    r = client.post("/api/examples", headers=admin, json={"title": "t", "files": {"main.py": MAIN, "helper.py": HELPER}})
    eid = r.json()["example_id"]
    r = client.put(f"/api/examples/{eid}", headers=admin, json={"title": "t2", "files": {"main.py": MAIN, "helper.py": HELPER + "\n"}})
    assert r.status_code == 200
    assert set(client.get(f"/api/examples/{eid}").json()["files"]) == {"main.py", "helper.py"}


def test_builtin_examples_unaffected(client):
    exs = client.get("/api/examples?model=all").json()["examples"]
    assert exs and all(e["files"] and e["entry_point"] for e in exs)
