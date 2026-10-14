"""
Per-user project workspaces (one folder per project) stored on disk.

Layout:  <WORKSPACES_DIR>/<username>/<project>/<files...>  (+ hidden .meta.json)

The directory lives outside the source tree by default so uvicorn --reload does not
restart the server when students save .py files.
"""

import json
import os
import re
import shutil
import datetime
from typing import Dict, List, Optional

import config

WORKSPACES_DIR = os.environ.get("UR_WORKSPACES_DIR") or getattr(
    config, "WORKSPACES_DIR", os.path.join(os.path.expanduser("~"), "ur_platform_workspaces")
)

META_NAME = ".meta.json"
ALLOWED_EXTENSIONS = {".py", ".txt", ".json", ".csv", ".md", ".log", ".yaml", ".yml"}
MAX_FILE_BYTES = 256 * 1024
MAX_FILES_PER_PROJECT = 60
MAX_PROJECTS_PER_USER = 50

_NAME_RE = re.compile(r"[^A-Za-z0-9_\-\. ]+")


class WorkspaceError(ValueError):
    pass


def _clean_segment(name: str, what: str) -> str:
    name = _NAME_RE.sub("_", (name or "").strip())
    name = name.strip(" .")
    if not name or name in (".", "..") or len(name) > 64:
        raise WorkspaceError(f"Invalid {what} name.")
    return name


def clean_project_name(name: str) -> str:
    return _clean_segment(name, "project")


def clean_filename(name: str) -> str:
    name = os.path.basename((name or "").strip().replace("\\", "/"))
    name = _NAME_RE.sub("_", name).strip()
    if not name or name.startswith("."):
        raise WorkspaceError(f"Invalid file name '{name}'.")
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise WorkspaceError(f"File type '{ext or 'none'}' not allowed. Use: {', '.join(sorted(ALLOWED_EXTENSIONS))}")
    return name


def _user_dir(username: str) -> str:
    return os.path.join(WORKSPACES_DIR, _clean_segment(username, "user"))


def _project_dir(username: str, project: str) -> str:
    return os.path.join(_user_dir(username), clean_project_name(project))


def _read_meta(pdir: str) -> Dict:
    try:
        with open(os.path.join(pdir, META_NAME), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_meta(pdir: str, meta: Dict) -> None:
    with open(os.path.join(pdir, META_NAME), "w", encoding="utf-8") as f:
        json.dump(meta, f)


def _list_files(pdir: str) -> Dict[str, str]:
    files: Dict[str, str] = {}
    for name in sorted(os.listdir(pdir)):
        path = os.path.join(pdir, name)
        if name.startswith(".") or not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                files[name] = f.read()
        except Exception:
            continue
    return files


def list_projects(username: str) -> List[Dict]:
    udir = _user_dir(username)
    if not os.path.isdir(udir):
        return []
    result = []
    for name in sorted(os.listdir(udir), key=str.lower):
        pdir = os.path.join(udir, name)
        if not os.path.isdir(pdir):
            continue
        meta = _read_meta(pdir)
        count = len([n for n in os.listdir(pdir) if not n.startswith(".") and os.path.isfile(os.path.join(pdir, n))])
        result.append({
            "name": name,
            "files_count": count,
            "entry_point": meta.get("entry_point", "main.py"),
            "robot_model": meta.get("robot_model"),
            "updated_at": datetime.datetime.fromtimestamp(os.path.getmtime(pdir)).isoformat(),
        })
    return result


def project_exists(username: str, project: str) -> bool:
    return os.path.isdir(_project_dir(username, project))


def create_project(username: str, project: str, files: Optional[Dict[str, str]] = None,
                   entry_point: str = "main.py", robot_model: Optional[str] = None,
                   template: Optional[str] = None) -> Dict:
    pname = clean_project_name(project)
    if os.path.isdir(_project_dir(username, pname)):
        raise WorkspaceError(f"A project named '{pname}' already exists.")
    if len(list_projects(username)) >= MAX_PROJECTS_PER_USER:
        raise WorkspaceError(f"Project limit reached ({MAX_PROJECTS_PER_USER}).")
    os.makedirs(_project_dir(username, pname), exist_ok=False)

    if not files:
        model = robot_model or template or "ur"
        if model == "niryo":
            init_code = '''from pyniryo import *
robot = NiryoRobot("10.0.130.49")
print("Connected to Niryo")
# Write your routine here
robot.close_connection()
'''
        else:
            init_code = '''import numpy as np
from rtde_control import RTDEControlInterface as RTDEControl
from rtde_receive import RTDEReceiveInterface as RTDEReceive

ROBOT_IP = "10.0.10.60"
rtde_c = RTDEControl(ROBOT_IP)
rtde_r = RTDEReceive(ROBOT_IP)

home_q = [-np.pi/2, -np.pi/2, -np.pi/2, -np.pi/2, np.pi/2, 0.0]
print("Moving to home joint configuration...")
rtde_c.moveJ(home_q, 0.5, 0.5)
rtde_c.stopScript()
print("Routine completed successfully!")
'''
        files = {"main.py": init_code}

    return save_project(username, pname, files, entry_point, robot_model or template)


def save_project(username: str, project: str, files: Dict[str, str],
                 entry_point: Optional[str] = None, robot_model: Optional[str] = None) -> Dict:
    """Replaces the project's files with `files` (files missing from the dict are deleted)."""
    pdir = _project_dir(username, project)
    os.makedirs(pdir, exist_ok=True)

    if len(files) > MAX_FILES_PER_PROJECT:
        raise WorkspaceError(f"Too many files (max {MAX_FILES_PER_PROJECT}).")
    cleaned: Dict[str, str] = {}
    for fname, content in files.items():
        cname = clean_filename(fname)
        content = content or ""
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise WorkspaceError(f"File '{cname}' is too large (max {MAX_FILE_BYTES // 1024} KB).")
        cleaned[cname] = content

    for existing in os.listdir(pdir):
        epath = os.path.join(pdir, existing)
        if existing.startswith(".") or not os.path.isfile(epath):
            continue
        if existing not in cleaned:
            os.remove(epath)
    for cname, content in cleaned.items():
        with open(os.path.join(pdir, cname), "w", encoding="utf-8") as f:
            f.write(content)

    meta = _read_meta(pdir)
    ep = (entry_point or meta.get("entry_point") or "main.py").strip()
    if ep not in cleaned:
        ep = "main.py" if "main.py" in cleaned else (next(iter(cleaned), "main.py"))
    meta["entry_point"] = ep
    if robot_model in ("ur", "niryo"):
        meta["robot_model"] = robot_model
    _write_meta(pdir, meta)
    return {"name": os.path.basename(pdir), "files": cleaned, "entry_point": ep, "robot_model": meta.get("robot_model")}


def load_project(username: str, project: str) -> Dict:
    pdir = _project_dir(username, project)
    if not os.path.isdir(pdir):
        raise WorkspaceError("Project not found.")
    meta = _read_meta(pdir)
    files = _list_files(pdir)
    ep = meta.get("entry_point", "main.py")
    if ep not in files and files:
        ep = "main.py" if "main.py" in files else next(iter(files))
    return {"name": os.path.basename(pdir), "files": files, "entry_point": ep, "robot_model": meta.get("robot_model")}


def delete_project(username: str, project: str) -> bool:
    pdir = _project_dir(username, project)
    if not os.path.isdir(pdir):
        return False
    shutil.rmtree(pdir, ignore_errors=True)
    return True


def rename_project(username: str, old_project: str, new_project: str) -> Dict:
    """Renames an existing project folder."""
    old_pname = clean_project_name(old_project)
    new_pname = clean_project_name(new_project)
    old_dir = _project_dir(username, old_pname)
    new_dir = _project_dir(username, new_pname)
    if not os.path.isdir(old_dir):
        raise WorkspaceError(f"Project '{old_pname}' does not exist.")
    if old_pname == new_pname:
        return load_project(username, new_pname)
    if os.path.isdir(new_dir):
        raise WorkspaceError(f"A project named '{new_pname}' already exists.")
    os.rename(old_dir, new_dir)
    return load_project(username, new_pname)



def merge_files(username: str, project: str, new_files: Dict[str, str]) -> Dict[str, str]:
    """Adds/overwrites files produced by a script run. Silently skips invalid names. Returns what was saved."""
    if not new_files:
        return {}
    pdir = _project_dir(username, project)
    if not os.path.isdir(pdir):
        return {}
    saved: Dict[str, str] = {}
    current_count = len([n for n in os.listdir(pdir) if not n.startswith(".")])
    for fname, content in new_files.items():
        try:
            cname = clean_filename(fname)
        except WorkspaceError:
            continue
        content = content or ""
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            continue
        if not os.path.exists(os.path.join(pdir, cname)):
            if current_count >= MAX_FILES_PER_PROJECT:
                continue
            current_count += 1
        with open(os.path.join(pdir, cname), "w", encoding="utf-8") as f:
            f.write(content)
        saved[cname] = content
    return saved
