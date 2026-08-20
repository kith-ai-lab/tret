"""Static safety scan for pack-authored method entrypoints.

WHAT THIS IS: a *deterrent and review aid*, not a security boundary. It parses
each method entrypoint with `ast` and rejects the obvious ways a deterministic
method could stop being deterministic — network clients, subprocesses, FFI,
dynamic import, and `eval`/`exec`/`compile`. Findings are reported as pack
validation ERRORS with file:line detail so a broken pack never installs.

WHAT THIS IS NOT: a sandbox. A determined author can defeat any AST check
(string-built attribute lookups, `getattr` chains, C extensions reached through
allowed modules, data-driven dispatch). The real controls are, in order:

  1. Packs are operator-installed content — treat installing a pack exactly
     like deploying code you reviewed.
  2. `tret/services/methods.py` runs methods in an isolated interpreter with
     rlimits, a wall clock, output caps, an empty environment, and — on Linux
     with `unshare` available — an empty network namespace.
  3. This scan, which catches accidents and raises the cost of casual abuse.

Only #1 and #2 are load-bearing. Do not present this scan to users as
sandboxing. See docs/hardening.md.
"""
from __future__ import annotations

import ast
from pathlib import Path

# Module (or module prefix) -> why it is refused. Matched against the full
# dotted name and against its root package, so "http.client" trips "http".
BANNED_MODULES: dict[str, str] = {
    # network clients / servers
    "socket": "network access",
    "socketserver": "network access",
    "ssl": "network access",
    "http": "network access",
    "urllib": "network access",
    "ftplib": "network access",
    "smtplib": "network access",
    "poplib": "network access",
    "imaplib": "network access",
    "nntplib": "network access",
    "telnetlib": "network access",
    "xmlrpc": "network access",
    "webbrowser": "network access",
    "asyncio": "network access and concurrency (methods are pure and synchronous)",
    "selectors": "socket multiplexing",
    # process spawning
    "subprocess": "process spawning",
    "multiprocessing": "process spawning",
    "pty": "process spawning",
    "runpy": "dynamic code execution",
    "code": "dynamic code execution",
    "codeop": "dynamic code execution",
    # FFI / native code loading
    "ctypes": "FFI / native code loading",
    "cffi": "FFI / native code loading",
    # dynamic import
    "importlib": "dynamic import",
    "imp": "dynamic import",
    # deserialisation that executes code
    "pickle": "deserialisation that can execute arbitrary code",
    "shelve": "deserialisation that can execute arbitrary code",
    "marshal": "deserialisation of code objects",
}

# Attribute calls on `os` that spawn or replace processes.
BANNED_OS_ATTRS: dict[str, str] = {
    "system": "shell execution",
    "popen": "shell execution",
    "fork": "process spawning",
    "forkpty": "process spawning",
    "kill": "signalling other processes",
    "killpg": "signalling other processes",
    "abort": "process control",
    "putenv": "environment mutation",
    "unsetenv": "environment mutation",
}
BANNED_OS_PREFIXES: dict[str, str] = {
    "exec": "process replacement",
    "spawn": "process spawning",
    "posix_spawn": "process spawning",
}

# Bare callables that turn data into code.
BANNED_CALLS: dict[str, str] = {
    "eval": "dynamic code execution",
    "exec": "dynamic code execution",
    "compile": "dynamic code execution",
    "__import__": "dynamic import",
}

# Attribute names used to escape the module namespace.
BANNED_DUNDER_ATTRS: dict[str, str] = {
    "__builtins__": "builtins escape",
    "__subclasses__": "class hierarchy escape",
    "__globals__": "frame/global escape",
    "__code__": "code object access",
    "__loader__": "import machinery access",
    "__mro__": "class hierarchy escape",
}


class _MethodScanner(ast.NodeVisitor):
    def __init__(self, label: str) -> None:
        self.label = label  # what to print before line numbers, e.g. "methods/x.py"
        self.violations: list[str] = []

    def _flag(self, node: ast.AST, message: str) -> None:
        self.violations.append(f"{self.label}:{getattr(node, 'lineno', 0)}: {message}")

    def _check_module(self, node: ast.AST, dotted: str) -> None:
        root = dotted.split(".", 1)[0]
        reason = BANNED_MODULES.get(dotted) or BANNED_MODULES.get(root)
        if reason:
            self._flag(node, f"imports '{dotted}' ({reason})")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_module(node, alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:  # relative import — methods are single files, no package
            self._flag(node, "uses a relative import (method entrypoints are single files)")
        elif node.module:
            self._check_module(node, node.module)
            if node.module == "os":
                for alias in node.names:
                    self._check_os_attr(node, alias.name)
        self.generic_visit(node)

    def _check_os_attr(self, node: ast.AST, attr: str) -> None:
        reason = BANNED_OS_ATTRS.get(attr)
        if reason is None:
            for prefix, why in BANNED_OS_PREFIXES.items():
                if attr.startswith(prefix):
                    reason = why
                    break
        if reason:
            self._flag(node, f"uses os.{attr} ({reason})")

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id in BANNED_CALLS:
            self._flag(node, f"calls {func.id}() ({BANNED_CALLS[func.id]})")
        if isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name) and func.value.id == "os":
                self._check_os_attr(node, func.attr)
            if func.attr in BANNED_CALLS:
                self._flag(node, f"calls {func.attr}() ({BANNED_CALLS[func.attr]})")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        reason = BANNED_DUNDER_ATTRS.get(node.attr)
        if reason:
            self._flag(node, f"accesses {node.attr} ({reason})")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        reason = BANNED_DUNDER_ATTRS.get(node.id)
        if reason:
            self._flag(node, f"accesses {node.id} ({reason})")
        self.generic_visit(node)


def scan_method_source(source: str, label: str) -> list[str]:
    """Scan one method entrypoint's source. Returns `label:line: reason` strings."""
    try:
        tree = ast.parse(source, filename=label)
    except SyntaxError as e:
        return [f"{label}:{e.lineno or 0}: does not parse as Python ({e.msg})"]
    scanner = _MethodScanner(label)
    scanner.visit(tree)
    return scanner.violations


def scan_method_file(path: Path, label: str | None = None) -> list[str]:
    """Scan a method entrypoint on disk. A missing/unreadable file is a violation."""
    label = label or path.name
    try:
        source = path.read_text()
    except OSError as e:
        return [f"{label}:0: cannot be read ({e.strerror or e})"]
    return scan_method_source(source, label)
