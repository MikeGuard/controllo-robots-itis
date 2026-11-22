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


MAX_PATH_DEPTH = 5


def clean_filename(name: str) -> str:
    """
    Validates a project file path. Nested folders are allowed ("lib/util.py"); absolute paths, '..'
    segments, hidden names and disallowed extensions are not. Returns the normalised relative path.
    """
    raw = (name or "").strip().replace("\\", "/")
    segs = [x for x in raw.split("/") if x not in ("", ".")]
    if not segs or len(segs) > MAX_PATH_DEPTH or raw.startswith("/") or ".." in segs:
        raise WorkspaceError(f"Invalid file name '{name}'.")
    cleaned = []
    for seg in segs:
        seg = _NAME_RE.sub("_", seg).strip()
        if not seg or seg.startswith(".") or len(seg) > 64:
            raise WorkspaceError(f"Invalid file name '{name}'.")
        cleaned.append(seg)
    ext = os.path.splitext(cleaned[-1])[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise WorkspaceError(f"File type '{ext or 'none'}' not allowed. Use: {', '.join(sorted(ALLOWED_EXTENSIONS))}")
    return "/".join(cleaned)


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


def detect_robot_model(text: str) -> str:
    """'niryo' when the code uses pyniryo, else 'ur'. The one rule used everywhere a model must be guessed."""
    return "niryo" if ("pyniryo" in (text or "") or "NiryoRobot" in (text or "")) else "ur"


_STARTER_MAIN = {
    "niryo": '''from pyniryo import *
robot = NiryoRobot("10.0.130.49")
print("Connected to Niryo")
# Write your routine here
robot.close_connection()
''',
    "ur": '''import numpy as np
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
''',
}


def starter_main_py(model: Optional[str]) -> str:
    """main.py of a brand-new (or emptied) project."""
    return _STARTER_MAIN["niryo" if model == "niryo" else "ur"]


def resolve_entry_point(files: Dict[str, str], wanted: Optional[str] = None) -> str:
    """The file a project runs: `wanted` if present, else main.py, else the first .py, else the first file.
    Same rule as Common.resolveEntry in static/js/common.js."""
    wanted = (wanted or "").strip()
    if wanted and wanted in files:
        return wanted
    if "main.py" in files:
        return "main.py"
    return next((f for f in files if f.endswith(".py")), next(iter(files), "main.py"))


def clean_files(files: Dict[str, str]) -> Dict[str, str]:
    """Validates a whole project file set (count, names, sizes). Raises WorkspaceError; returns cleaned names."""
    if len(files) > MAX_FILES_PER_PROJECT:
        raise WorkspaceError(f"Too many files (max {MAX_FILES_PER_PROJECT}).")
    cleaned: Dict[str, str] = {}
    for fname, content in files.items():
        cname = clean_filename(fname)
        content = content or ""
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise WorkspaceError(f"File '{cname}' is too large (max {MAX_FILE_BYTES // 1024} KB).")
        cleaned[cname] = content
    return cleaned


def _write_atomic(target: str, content: str) -> None:
    """Writes via a hidden temp file + rename, so a crash never leaves a half-written (or visible .tmp) file."""
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = os.path.join(os.path.dirname(target), f".{os.path.basename(target)}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, target)


def _iter_project_files(pdir: str):
    """Yields (relative posix path, absolute path) for every visible file, nested folders included.
    Leftover '*.tmp' files from an interrupted save are not project files (and their extension is not allowed)."""
    for root, dirs, names in os.walk(pdir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for n in sorted(names):
            if n.startswith(".") or n.endswith(".tmp"):
                continue
            ap = os.path.join(root, n)
            yield os.path.relpath(ap, pdir).replace(os.sep, "/"), ap


def _list_files(pdir: str) -> Dict[str, str]:
    files: Dict[str, str] = {}
    for rel, path in _iter_project_files(pdir):
        try:
            with open(path, "r", encoding="utf-8") as f:
                files[rel] = f.read()
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
        count = sum(1 for _ in _iter_project_files(pdir))
        robot_model = meta.get("robot_model")
        if not robot_model:
            # Fallback inspection: check main.py
            main_path = os.path.join(pdir, meta.get("entry_point", "main.py"))
            try:
                with open(main_path, "r", encoding="utf-8") as mf:
                    robot_model = detect_robot_model(mf.read())
            except Exception:
                robot_model = "ur"
            meta["robot_model"] = robot_model
            _write_meta(pdir, meta)

        result.append({
            "name": name,
            "files_count": count,
            "entry_point": meta.get("entry_point", "main.py"),
            "robot_model": robot_model,
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
    files = clean_files(files) if files else {"main.py": starter_main_py(robot_model or template)}
    os.makedirs(_project_dir(username, pname), exist_ok=False)      # only once the files are known to be valid
    return save_project(username, pname, files, entry_point, robot_model or template)


def save_project(username: str, project: str, files: Dict[str, str],
                 entry_point: Optional[str] = None, robot_model: Optional[str] = None) -> Dict:
    """Replaces the project's files with `files` (files missing from the dict are deleted)."""
    pdir = _project_dir(username, project)
    cleaned = clean_files(files)            # validate everything before touching the disk
    os.makedirs(pdir, exist_ok=True)

    # Write first (atomically, via hidden temp file + rename), delete stale files last, so a failure
    # part-way through can never leave a project with fewer files than it started with.
    for cname, content in cleaned.items():
        _write_atomic(os.path.join(pdir, *cname.split("/")), content)
    for rel, path in list(_iter_project_files(pdir)):
        if rel not in cleaned:
            os.remove(path)
    for root, dirs, names in os.walk(pdir, topdown=False):
        for n in names:                                          # leftovers of an interrupted older save
            if n.endswith(".tmp"):
                os.remove(os.path.join(root, n))
        if root != pdir and not os.listdir(root):                # prune folders left empty
            os.rmdir(root)

    meta = _read_meta(pdir)
    ep = resolve_entry_point(cleaned, entry_point or meta.get("entry_point"))
    if robot_model in ("ur", "niryo"):
        meta["robot_model"] = robot_model
    elif "robot_model" not in meta or not meta["robot_model"]:
        meta["robot_model"] = "ur"
    meta["entry_point"] = ep
    _write_meta(pdir, meta)
    return {"name": os.path.basename(pdir), "files": cleaned, "entry_point": ep, "robot_model": meta.get("robot_model")}


def load_project(username: str, project: str) -> Dict:
    pdir = _project_dir(username, project)
    if not os.path.isdir(pdir):
        raise WorkspaceError("Project not found.")
    meta = _read_meta(pdir)
    files = _list_files(pdir)
    robot_model = meta.get("robot_model") or "ur"
    if not files:
        files = {"main.py": starter_main_py(robot_model)}
        save_project(username, project, files, "main.py", robot_model)
    ep = resolve_entry_point(files, meta.get("entry_point"))
    return {"name": os.path.basename(pdir), "files": files, "entry_point": ep, "robot_model": robot_model, "owner": username}


def list_all_users_projects() -> List[Dict]:
    if not os.path.isdir(WORKSPACES_DIR):
        return []
    all_projs: List[Dict] = []
    for uname in sorted(os.listdir(WORKSPACES_DIR), key=str.lower):
        udir = os.path.join(WORKSPACES_DIR, uname)
        if uname.startswith(".") or not os.path.isdir(udir):
            continue
        try:
            for p in list_projects(uname):
                all_projs.append({**p, "owner": uname})
        except Exception:
            continue
    return all_projs


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
    current_count = sum(1 for _ in _iter_project_files(pdir))
    for fname, content in new_files.items():
        try:
            cname = clean_filename(fname)
        except WorkspaceError:
            continue
        content = content or ""
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            continue
        target = os.path.join(pdir, *cname.split("/"))
        if not os.path.exists(target):
            if current_count >= MAX_FILES_PER_PROJECT:
                continue
            current_count += 1
        _write_atomic(target, content)
        saved[cname] = content
    return saved
