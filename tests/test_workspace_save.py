import pytest, workspace


def test_failed_save_never_loses_existing_files():
    workspace.save_project("u", "p", {"main.py": "a", "helper.py": "b"})
    with pytest.raises(workspace.WorkspaceError):
        workspace.save_project("u", "p", {"main.py": "NEW", "evil.exe": "x"})   # one bad file rejects the whole save
    saved = workspace.load_project("u", "p")["files"]
    assert saved == {"main.py": "a", "helper.py": "b"}


def test_save_replaces_and_removes_stale_files():
    workspace.save_project("u", "q", {"main.py": "1", "old.py": "2"})
    workspace.save_project("u", "q", {"main.py": "3"})
    assert workspace.load_project("u", "q")["files"] == {"main.py": "3"}
