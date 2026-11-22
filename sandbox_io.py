"""
Sandboxed file I/O for student scripts.

Student code may call open() to create/read/write text files, but only inside
its own project directory (the process run directory). Library code (numpy,
pyniryo, ur_rtde, ...) keeps using the real open() unrestricted.

Used by:
  * execution_engine.py  (loaded by file path from the robot run's supervisor script)
  * simulator.py         (installed inside the isolated simulation worker)

Also: writing a project into a run / simulation folder (write_project_files) and finding the files the program
produced there afterwards (collect_outputs), the same way for both.
"""

import builtins
import os
import time
from typing import Dict, Iterable, List, Optional

MAX_OUTPUT_FILES = 40
MAX_OUTPUT_FILE_BYTES = 256 * 1024
SKIP_NAMES = {"__pycache__", ".DS_Store"}


def safe_relpath(name: str) -> Optional[str]:
    """Normalised relative path for a project file inside a run / simulation directory, or None if it would
    escape it. Nested folders are kept ('lib/util.py'), so imports like `from lib.util import f` work the same
    in the simulator and on the robot."""
    raw = (name or "").strip().replace("\\", "/")
    segs = [x for x in raw.split("/") if x not in ("", ".")]
    if not segs or ".." in segs or raw.startswith("/") or any(x.startswith("~") for x in segs):
        return None
    return "/".join(segs)


def write_project_files(root: str, files: Optional[Dict[str, str]]) -> float:
    """Writes every project file under `root`, keeping its folders. Unsafe names are skipped.

    The files are given a modification time a little in the past. The returned value is the cut-off to pass to
    collect_outputs(since=...): a file modified after it was (re)written by the program, even when the program
    wrote exactly the same text again."""
    past = time.time() - 10
    for name, content in (files or {}).items():
        rel = safe_relpath(name)
        if not rel:
            continue
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content or "")
        os.utime(path, (past, past))
    return past + 5


def install(root: str, student_filenames: Iterable[str] = ()) -> None:
    """
    Replace builtins.open with a wrapper that confines *student* callers to `root`.
    A caller is considered "student code" when its source file lives inside `root`
    or its code object filename is listed in `student_filenames` (e.g. "<string>").
    """
    import sys

    root_real = os.path.realpath(root)
    student_names = set(student_filenames)
    real_open = getattr(builtins, "_sandbox_real_open", None) or builtins.open
    builtins._sandbox_real_open = real_open

    def is_student_caller() -> bool:
        try:
            fname = sys._getframe(2).f_code.co_filename
        except Exception:
            return False
        if fname in student_names:
            return True
        try:
            real = os.path.realpath(fname)
        except Exception:
            return False
        return real == root_real or real.startswith(root_real + os.sep)

    def safe_open(file, mode="r", *args, **kwargs):
        if not is_student_caller():
            return real_open(file, mode, *args, **kwargs)
        if isinstance(file, int):
            raise PermissionError("File descriptors are not allowed.")
        path = os.fspath(file)
        if isinstance(path, bytes):
            path = path.decode("utf-8", errors="replace")
        full = os.path.realpath(os.path.join(root_real, path))
        if full != root_real and not full.startswith(root_real + os.sep):
            raise PermissionError(
                f"Access denied: '{path}' is outside your project folder. Use relative file names like 'notes.txt'."
            )
        if any(c in mode for c in "wax+"):
            parent = os.path.dirname(full)
            if parent:
                os.makedirs(parent, exist_ok=True)
        return real_open(full, mode, *args, **kwargs)

    builtins.open = safe_open


def collect_outputs(
    root: str,
    original_files: Optional[Dict[str, str]] = None,
    skip: Iterable[str] = (),
    since: Optional[float] = None,
    allowed_ext: Optional[Iterable[str]] = None,
    skipped: Optional[List[str]] = None,
) -> Dict[str, str]:
    """
    Text files the program produced in `root`, sub-folders included ('data/log.txt'): new files, files whose
    text changed and, when `since` is given (see write_project_files), files it wrote again after `since` even
    with unchanged text, so a file the program keeps overwriting is always handed back.
    With `allowed_ext`, files of other types are not returned but listed in `skipped` (a project cannot keep
    them). Binary / oversized files, hidden files, caches and the names in `skip` are ignored.
    """
    original_files = {safe_relpath(k) or k: v for k, v in (original_files or {}).items()}
    skip_set = set(skip) | SKIP_NAMES
    allowed = {e.lower() for e in allowed_ext} if allowed_ext is not None else None
    outputs: Dict[str, str] = {}
    root_real = os.path.realpath(root)
    for dirpath, dirnames, filenames in os.walk(root_real):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_NAMES)
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root_real).replace(os.sep, "/")
            if name.startswith(".") or name.endswith(".pyc") or name in skip_set or rel in skip_set:
                continue
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            try:
                info = os.stat(path)
                if info.st_size > MAX_OUTPUT_FILE_BYTES:
                    continue
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue
            unchanged = rel in original_files and (original_files[rel] or "") == content
            rewritten = since is not None and info.st_mtime > since
            if unchanged and not rewritten:
                continue
            if allowed is not None and os.path.splitext(name)[1].lower() not in allowed:
                if skipped is not None:
                    skipped.append(rel)
                continue
            outputs[rel] = content
            if len(outputs) >= MAX_OUTPUT_FILES:
                return outputs
    return outputs
