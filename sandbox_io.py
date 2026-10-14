"""
Sandboxed file I/O for student scripts.

Student code may call open() to create/read/write text files, but only inside
its own project directory (the process run directory). Library code (numpy,
pyniryo, ur_rtde, ...) keeps using the real open() unrestricted.

Used by:
  * execution_engine.py  (loaded by file path from the generated script prelude)
  * simulator.py         (installed inside the isolated simulation worker)
"""

import builtins
import os
from typing import Dict, Iterable, Optional

MAX_OUTPUT_FILES = 40
MAX_OUTPUT_FILE_BYTES = 256 * 1024
SKIP_NAMES = {"__pycache__", ".DS_Store"}


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
    skip: Iterable[str] = ()
) -> Dict[str, str]:
    """
    Returns top-level text files in `root` that are new or modified compared to `original_files`.
    Binary / oversized files and the names in `skip` are ignored.
    """
    original_files = original_files or {}
    skip_set = set(skip) | SKIP_NAMES
    outputs: Dict[str, str] = {}
    try:
        names = sorted(os.listdir(root))
    except Exception:
        return outputs

    for name in names:
        if name in skip_set or name.startswith(".") or name.endswith(".pyc"):
            continue
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        try:
            if os.path.getsize(path) > MAX_OUTPUT_FILE_BYTES:
                continue
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue
        if name in original_files and (original_files[name] or "") == content:
            continue
        outputs[name] = content
        if len(outputs) >= MAX_OUTPUT_FILES:
            break
    return outputs
