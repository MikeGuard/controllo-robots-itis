"""Robot runs (execution engine): the student's file runs unchanged (its own line numbers), nothing of the
supervisor leaks into it, and every file the program writes comes back to the project. No robot is contacted."""
import asyncio
import os
import time

import pytest

import database
import execution_engine
import workspace


@pytest.fixture()
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "runs.db"))
    monkeypatch.setattr(execution_engine, "_send_ur_emergency_halt", lambda *a, **k: None)   # never reach a robot
    asyncio.run(database.init_db())

    def _run(files, entry="main.py"):
        async def go():
            eng = execution_engine.ExecutionEngine()
            sid = await database.create_submission("u", "p", files.get(entry, ""), files=files, entry_point=entry)
            out, saved = [], {}

            async def keep(outputs):
                saved.update(outputs)
            ok = await eng.execute_submission(sid, files.get(entry, ""), log_callback=out.append, robot_model="ur",
                                              files=files, entry_point=entry, timeout_sec=30, on_outputs=keep)
            return ok, "".join(out), saved
        return asyncio.run(go())
    return _run


def test_error_line_numbers_are_the_students_own(run):
    ok, log, _ = run({"main.py": "print('start')\n" + "\n" * 18 + "x = 1 / 0\n"})          # error on line 20
    assert not ok
    assert 'main.py", line 20' in log and "ZeroDivisionError" in log
    assert execution_engine.SUPERVISOR_SCRIPT not in log and "runpy" not in log   # no supervisor lines in the traceback


def test_supervisor_imports_are_not_visible_to_student_code(run):
    ok, log, _ = run({"main.py": "print([n for n in ('os', 'sys', 'builtins', 'runpy') if n in globals()])\nos.getcwd()\n"})
    assert not ok
    assert "[]" in log and "NameError: name 'os' is not defined" in log


def test_constants_printf_and_other_files_work_as_before(run):
    files = {"main.py": "import helper\nprint(__name__, __file__.endswith('main.py'), ROBOT_MODEL)\nhelper.f()\n",
             "helper.py": "def f():\n    printf('%s=%d', 'x', 3)\n    print('from helper')\n"}
    ok, log, _ = run(files)
    assert ok, log
    assert "__main__ True ur" in log and "x=3" in log and "from helper" in log


def test_entry_file_in_a_subfolder_imports_its_neighbours(run):
    ok, log, _ = run({"app/run.py": "import tools\ntools.hi()\n", "app/tools.py": "def hi():\n    print('nested entry ok')\n"},
                     entry="app/run.py")
    assert ok and "nested entry ok" in log


def test_every_file_the_program_writes_comes_back(run):
    files = {"main.py": "import w\nw.go()\n", "same.txt": "unchanged",
             "w.py": "def go():\n"
                     "    open('same.txt', 'w').write('unchanged')\n"       # rewritten with identical text
                     "    open('new.txt', 'w').write('fresh')\n"
                     "    open('data/log.txt', 'w').write('nested')\n"      # in a sub-folder
                     "    open('blob.dat', 'w').write('x')\n"}              # a type a project cannot keep
    ok, log, saved = run(files)
    assert ok, log
    assert saved == {"same.txt": "unchanged", "new.txt": "fresh", "data/log.txt": "nested"}
    assert "Not saved (file type not allowed in a project): blob.dat" in log


def test_files_written_by_a_main_run_are_saved_to_the_students_working_copy(client, admin, monkeypatch):
    import app as app_module
    monkeypatch.setattr(execution_engine, "_send_ur_emergency_halt", lambda *a, **k: None)
    sent = []

    async def to_student(name, payload):
        sent.append((name, payload))
    monkeypatch.setattr(app_module, "broadcast_to_student", to_student)
    files = {"main.py": "open('result.txt', 'w').write('42')\nprint('done')\n", "result.txt": "old"}
    workspace.save_project("alice", "lab", files)
    r = client.post("/api/submit", json={"student_name": "alice", "program_name": "lab", "project_name": "lab", "task_id": "lab",
                                          "code": files["main.py"], "files": files, "entry_point": "main.py",
                                          "robot_model": "ur", "commit_message": "run"})
    sid = r.json()["submission_id"]
    assert client.post(f"/api/submissions/{sid}/approve", headers=admin).status_code == 200
    deadline = time.time() + 20
    while time.time() < deadline:
        sub = client.get(f"/api/submissions/{sid}", headers=admin).json()
        if sub["status"] not in ("pending", "running") and "generated file" in (sub["logs"] or ""):
            break
        time.sleep(0.1)
    assert sub["status"] == "completed", sub["logs"]
    assert workspace.load_project("alice", "lab")["files"]["result.txt"] == "42"
    # the student's open editor is told, so their next Save cannot write the old text back
    assert ("alice", {"type": "files_generated", "project": "lab", "submission_id": sid, "files": {"result.txt": "42"}}) in sent
    assert not any(n.startswith(".") for n in os.listdir(os.path.join(workspace.WORKSPACES_DIR, "alice", "lab")) if n != ".meta.json")


def test_pages_never_mix_new_html_with_old_cached_scripts(client):
    """After an update the pages must load the new JS: assets are revalidated and the page links are versioned."""
    for page in ("/", "/admin"):
        r = client.get(page)
        assert r.status_code == 200 and "no-store" in r.headers["cache-control"]
        assert 'src="/static/js/common.js?v=' in r.text and 'href="/static/design.css?v=' in r.text
    js = client.get("/static/js/common.js")
    assert js.status_code == 200 and js.headers["cache-control"] == "no-cache"
