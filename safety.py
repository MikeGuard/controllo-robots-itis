"""
AST-based safety analysis and syntax validation for student submitted Python scripts.
"""

import ast
from typing import List, Tuple, Optional, Set

FORBIDDEN_MODULES = {
    "os", "sys", "subprocess", "shutil", "importlib", "http",
    "urllib", "requests", "ctypes", "pty", "multiprocessing", "threading",
    "pickle", "builtins", "signal", "posix", "gc"
}

FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "__import__", "globals",
    "delattr", "setattr"
}

FORBIDDEN_ATTRS = {
    "__subclasses__", "__bases__", "__mro__", "__globals__", "__code__"
}

ALLOWED_MODULES = {
    "math", "time", "random", "numpy", "typing", "ur_wrapper",
    "rtde_control", "rtde_receive", "pyniryo", "config", "socket", "rtde_io", "ur_rtde",
    "json", "csv", "datetime"
}


class SafetyVisitor(ast.NodeVisitor):
    def __init__(self, allowed_extra_modules: Optional[Set[str]] = None):
        self.errors: List[str] = []
        self.allowed_extra_modules = {m.strip() for m in (allowed_extra_modules or set()) if m.strip()}

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            root_pkg = alias.name.split(".")[0]
            if root_pkg in FORBIDDEN_MODULES:
                self.errors.append(
                    f"Line {node.lineno}: Dangerous or forbidden import '{alias.name}' is prohibited."
                )
            elif (root_pkg not in ALLOWED_MODULES) and (root_pkg not in self.allowed_extra_modules):
                allowed_display = sorted(ALLOWED_MODULES.union(self.allowed_extra_modules))
                self.errors.append(
                    f"Line {node.lineno}: Unauthorized import '{alias.name}'. Only safe modules or project files ({', '.join(allowed_display)}) are allowed."
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module:
            root_pkg = node.module.split(".")[0]
            if root_pkg in FORBIDDEN_MODULES:
                self.errors.append(
                    f"Line {node.lineno}: Dangerous or forbidden import from '{node.module}' is prohibited."
                )
            elif (root_pkg not in ALLOWED_MODULES) and (root_pkg not in self.allowed_extra_modules):
                self.errors.append(
                    f"Line {node.lineno}: Unauthorized import from '{node.module}'. Only safe modules or project files are allowed."
                )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_CALLS:
                self.errors.append(
                    f"Line {node.lineno}: Dangerous built-in call '{node.func.id}()' is prohibited."
                )
            elif node.func.id == "open":
                # Files may only be created inside the project folder (enforced again at runtime)
                first = node.args[0] if node.args else None
                if first is None:
                    for kw in node.keywords:
                        if kw.arg == "file":
                            first = kw.value
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    p = first.value.replace("\\", "/")
                    if p.startswith(("/", "~")) or ":" in p or ".." in p.split("/"):
                        self.errors.append(
                            f"Line {node.lineno}: open('{first.value}') is outside the project folder. Use a simple relative name like 'notes.txt'."
                        )
                elif isinstance(first, ast.Constant) and not isinstance(first.value, str):
                    self.errors.append(f"Line {node.lineno}: open() requires a file name string.")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        if node.attr in FORBIDDEN_ATTRS:
            self.errors.append(
                f"Line {node.lineno}: Accessing reflection attribute '{node.attr}' is prohibited."
            )
        self.generic_visit(node)


def validate_python_code(code_str: str, project_modules: Optional[Set[str]] = None) -> Tuple[bool, List[str]]:
    """
    Validates Python syntax and checks for prohibited operations using AST traversal.
    Allows local project modules if provided in project_modules.
    Returns (is_valid, list_of_errors).
    """
    errors = []
    try:
        tree = ast.parse(code_str)
    except SyntaxError as e:
        return False, [f"Syntax Error at line {e.lineno}, col {e.offset}: {e.msg}"]
    except Exception as e:
        return False, [f"Failed to parse code: {str(e)}"]

    visitor = SafetyVisitor(allowed_extra_modules=project_modules)
    visitor.visit(tree)
    if visitor.errors:
        return False, visitor.errors

    return True, []
