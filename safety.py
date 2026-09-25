"""
AST-based safety analysis and syntax validation for student submitted Python scripts.
"""

import ast
from typing import List, Tuple

FORBIDDEN_MODULES = {
    "os", "sys", "subprocess", "shutil", "importlib", "socket", "http",
    "urllib", "requests", "ctypes", "pty", "multiprocessing", "threading",
    "pickle", "builtins", "signal", "posix", "gc"
}

FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "open", "__import__", "globals", "locals",
    "delattr", "setattr"
}

FORBIDDEN_ATTRS = {
    "__subclasses__", "__bases__", "__mro__", "__globals__", "__code__"
}

ALLOWED_MODULES = {
    "math", "time", "random", "numpy", "typing", "ur_wrapper",
    "rtde_control", "rtde_receive", "pyniryo", "config"
}


class SafetyVisitor(ast.NodeVisitor):
    def __init__(self):
        self.errors: List[str] = []

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            root_pkg = alias.name.split(".")[0]
            if root_pkg in FORBIDDEN_MODULES or (root_pkg not in ALLOWED_MODULES):
                self.errors.append(
                    f"Line {node.lineno}: Unauthorized import '{alias.name}'. Only safe modules ({', '.join(sorted(ALLOWED_MODULES))}) are allowed."
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module:
            root_pkg = node.module.split(".")[0]
            if root_pkg in FORBIDDEN_MODULES or (root_pkg not in ALLOWED_MODULES):
                self.errors.append(
                    f"Line {node.lineno}: Unauthorized import from '{node.module}'. Only safe modules are allowed."
                )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_CALLS:
                self.errors.append(
                    f"Line {node.lineno}: Dangerous built-in call '{node.func.id}()' is prohibited."
                )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        if node.attr in FORBIDDEN_ATTRS:
            self.errors.append(
                f"Line {node.lineno}: Accessing reflection attribute '{node.attr}' is prohibited."
            )
        self.generic_visit(node)


def validate_python_code(code_str: str) -> Tuple[bool, List[str]]:
    """
    Validates Python syntax and checks for prohibited operations using AST traversal.
    Returns (is_valid, list_of_errors).
    """
    errors = []
    try:
        tree = ast.parse(code_str)
    except SyntaxError as e:
        return False, [f"Syntax Error at line {e.lineno}, col {e.offset}: {e.msg}"]
    except Exception as e:
        return False, [f"Failed to parse code: {str(e)}"]

    visitor = SafetyVisitor()
    visitor.visit(tree)
    if visitor.errors:
        return False, visitor.errors

    return True, []
