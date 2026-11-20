"""The child side of the unvetted analysis lane (R08), and the static checks.

This file is BOTH imported by `tret.services.analysis` (for `validate_code`, the
AST allow-list the parent applies before anything is spawned) and executed as a
standalone script (`python -I analysis_runner.py`) inside the sandbox, where it
re-applies the same checks (defence in depth: the child never trusts that the
parent validated) and then runs the model's code with a minimal builtin set.

It must stay stdlib-only and free of tret imports: it runs under `python -I`
with an empty environment.

Contract (stdin -> stdout):
  stdin : {"code": "<python source>", "inputs": {name: [row, ...]}}
  stdout: {"rows": [...]} — printed by the model's code, last line wins.

Layers, in the order they apply. NONE of these is the security boundary: the
boundary is the bubblewrap sandbox the parent runs this file in (see analysis.py).
Everything here is defence in depth, because language-level filters of Python have
been escaped before (`match` class patterns read attributes with a real getattr and
bypass the underscore rule, which is why `match` is now refused outright):

  1. AST allow-list: only whitelisted imports; no name from FORBIDDEN_NAMES; no
     identifier or attribute beginning with an underscore (this is what closes
     `().__class__.__bases__[0].__subclasses__()` and every dunder route);
     no attribute from FORBIDDEN_ATTRS (frame/code/traceback objects and the
     two str methods that can walk attributes from a string).
  2. A positive builtin allow-list: the code runs with a hand-built `__builtins__`
     that has no `open`, `getattr`, `type`, `compile`, ...
  3. A guarded `__import__` that hands out *snapshot namespaces* of the allowed
     modules with every module-typed attribute removed, so `json.codecs.open`
     style reaches through an allowed module do not exist.
"""
from __future__ import annotations

import ast
import json
import sys
import types

ALLOWED_MODULES = frozenset(
    {
        "math",
        "statistics",
        "json",
        "csv",
        "decimal",
        "fractions",
        "collections",
        "itertools",
        "functools",
        "datetime",
        "re",
    }
)

# Names the code may not even mention. Several are simply absent from the
# runtime builtins too; listing them gives the model a precise, early error.
FORBIDDEN_NAMES = frozenset(
    {
        "open", "exec", "eval", "compile", "__import__", "getattr", "setattr",
        "delattr", "globals", "locals", "vars", "input", "breakpoint", "type",
        "object", "super", "dir", "memoryview", "bytearray", "help", "exit",
        "quit", "classmethod", "staticmethod", "property", "id", "copyright",
        "credits", "license", "print_exception",
    }
)

# Attributes that reach interpreter internals without containing an underscore,
# plus the two str methods that evaluate attribute paths from a *string*
# (`"{0.__class__}".format(x)` never shows `__class__` to the AST).
FORBIDDEN_ATTRS = frozenset({"format", "format_map", "mro", "with_traceback"})

# Names inside the allowed modules that read or write attributes by *string name*
# or rebuild objects around them (functools.update_wrapper(h, f, assigned=("__globals__",))
# copies any attribute the caller names). They are dropped from the module snapshots
# at runtime and refused in the source (as attribute or from-import name).
DENIED_MODULE_ATTRS = {
    "functools": frozenset(
        {
            "update_wrapper", "wraps", "WRAPPER_ASSIGNMENTS", "WRAPPER_UPDATES",
            "partialmethod", "singledispatch", "singledispatchmethod", "cached_property",
            "total_ordering",
        }
    ),
    "operator": frozenset({"attrgetter", "methodcaller"}),
}
_DENIED_ANY = frozenset().union(*DENIED_MODULE_ATTRS.values())
FORBIDDEN_ATTRS = FORBIDDEN_ATTRS | _DENIED_ANY
FORBIDDEN_ATTR_PREFIXES = ("gi_", "cr_", "ag_", "f_", "tb_", "co_", "func_")

MAX_CODE_CHARS = 20_000

SAFE_BUILTIN_NAMES = (
    "abs all any ascii bin bool bytes callable chr complex dict divmod enumerate "
    "filter float format frozenset hash hex int isinstance issubclass iter len list "
    "map max min next oct ord pow print range repr reversed round set slice sorted "
    "str sum tuple zip "
    "Exception ArithmeticError AssertionError AttributeError IndexError KeyError "
    "LookupError NameError OverflowError RuntimeError StopIteration TypeError "
    "ValueError ZeroDivisionError ImportError "
    "True False None"
).split()
# `format` above is the builtin FUNCTION (value, spec); it cannot walk attributes.


# `match` class/mapping/sequence patterns read attributes through a real getattr
# that the underscore rule never sees. Refuse the whole family, plus the PEP 695
# type-parameter syntax whose names this walker does not inspect.
_REFUSED_NODES = tuple(
    getattr(ast, n)
    for n in (
        "Match", "match_case", "MatchValue", "MatchSingleton", "MatchSequence", "MatchMapping",
        "MatchClass", "MatchStar", "MatchAs", "MatchOr",
        "TypeAlias", "TypeVar", "ParamSpec", "TypeVarTuple",
    )
    if hasattr(ast, n)
)


def validate_code(code: str) -> list[str]:
    """Every reason `code` may not run; empty means it passed the static checks."""
    if not isinstance(code, str) or not code.strip():
        return ["code is empty"]
    if len(code) > MAX_CODE_CHARS:
        return [f"code is {len(code)} characters; the limit is {MAX_CODE_CHARS}"]
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        return [f"syntax error at line {e.lineno}: {e.msg}"]
    problems: list[str] = []

    def bad(node: ast.AST, msg: str) -> None:
        problems.append(f"line {getattr(node, 'lineno', '?')}: {msg}")

    def check_ident(node: ast.AST, ident: str, what: str) -> None:
        if ident.startswith("_"):
            bad(node, f"{what} '{ident}' starts with an underscore; names that start with _ are not allowed")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_MODULES:
                    bad(node, f"import of '{alias.name}' is not allowed (allowed: {', '.join(sorted(ALLOWED_MODULES))})")
                if alias.asname:
                    check_ident(node, alias.asname, "alias")
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "") not in ALLOWED_MODULES:
                bad(node, f"import from '{'.' * node.level}{node.module or ''}' is not allowed (allowed: {', '.join(sorted(ALLOWED_MODULES))})")
            for alias in node.names:
                check_ident(node, alias.name, "imported name")
                if alias.name in _DENIED_ANY:
                    bad(node, f"imported name '{alias.name}' is not allowed")
                if alias.asname:
                    check_ident(node, alias.asname, "alias")
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                bad(node, f"'{node.id}' is not allowed")
            check_ident(node, node.id, "name")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRS or node.attr.startswith(FORBIDDEN_ATTR_PREFIXES):
                bad(node, f"attribute '.{node.attr}' is not allowed")
            check_ident(node, node.attr, "attribute")
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            check_ident(node, node.name, "definition")
            if isinstance(node, ast.ClassDef) and node.keywords:
                bad(node, "class keywords (metaclass tricks) are not allowed")
        elif isinstance(node, ast.arg):
            check_ident(node, node.arg, "parameter")
        elif isinstance(node, ast.keyword):
            if node.arg:
                check_ident(node, node.arg, "keyword argument")
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                check_ident(node, node.name, "name")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bad(node, "'global' and 'nonlocal' are not allowed")
        elif isinstance(node, _REFUSED_NODES):
            bad(node, f"{type(node).__name__} is not allowed (structural pattern matching reads "
                      "attributes by name; use if/elif)")
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith)):
            bad(node, "async code is not allowed")
    return problems


def _snapshot(module: types.ModuleType) -> types.SimpleNamespace:
    """The module's public non-module attributes, copied: no way back to the
    real module object or to the modules it imported itself. Limited to the
    module's `__all__` when it has one (so helpers it merely imported, such as
    statistics' `itemgetter`, are not re-exported), minus DENIED_MODULE_ATTRS."""
    names = getattr(module, "__all__", None)
    source = {k: getattr(module, k) for k in names if hasattr(module, k)} if names else vars(module)
    denied = DENIED_MODULE_ATTRS.get(module.__name__, frozenset())
    return types.SimpleNamespace(
        **{
            k: v
            for k, v in source.items()
            if not k.startswith("_") and k not in denied and not isinstance(v, types.ModuleType)
        }
    )


def _make_import(real_import, cache: dict):
    def guarded(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        if level or name not in ALLOWED_MODULES:
            raise ImportError(f"import of '{name}' is not allowed")
        if name not in cache:
            cache[name] = _snapshot(real_import(name))
        ns = cache[name]
        for item in fromlist or ():
            if item != "*" and not hasattr(ns, item):
                raise ImportError(f"cannot import name '{item}' from '{name}'")
        return ns

    return guarded


def _safe_builtins(real_builtins, guarded_import) -> dict:
    out = {n: getattr(real_builtins, n) for n in SAFE_BUILTIN_NAMES if hasattr(real_builtins, n)}
    out["__import__"] = guarded_import
    out["__build_class__"] = real_builtins.__build_class__  # needed by `class` statements
    out["__name__"] = "analysis"
    return out


def main() -> int:
    import builtins

    payload = json.load(sys.stdin)
    code = payload.get("code")
    problems = validate_code(code)
    if problems:
        sys.stderr.write("rejected by the child-side code check:\n" + "\n".join(problems[:20]) + "\n")
        return 3
    inputs = payload.get("inputs") or {}
    real_import = builtins.__import__
    env = {
        "__builtins__": _safe_builtins(builtins, _make_import(real_import, {})),
        "__name__": "analysis",
        "inputs": inputs,
    }
    # Only stdout carries the contract; leave the real stdout to print(), and
    # drop the parent's references so the code has nothing to reach.
    del real_import, payload
    exec(compile(code, "<analysis>", "exec"), env)  # noqa: S102 - the point of this file
    return 0


if __name__ == "__main__":
    sys.exit(main())
