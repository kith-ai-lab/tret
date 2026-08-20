"""The static method safety scan — a deterrent, enforced at pack validation."""
from pathlib import Path

from tret.packs.loader import validate_pack
from tret.packs.safety import scan_method_file, scan_method_source

PACKS_DIR = Path(__file__).parent.parent.parent / "packs"

CLEAN = """
import json
import sys
import math
from collections import defaultdict


def main() -> None:
    payload = json.load(sys.stdin)
    rows = [{"n": math.sqrt(len(payload))}]
    json.dump({"rows": rows}, sys.stdout)
"""


def test_clean_method_passes():
    assert scan_method_source(CLEAN, "methods/clean.py") == []


def test_shipped_pack_methods_pass():
    for path in (PACKS_DIR / "climate-risk/methods").glob("*.py"):
        assert scan_method_file(path) == [], path


def test_network_imports_are_flagged_with_line_numbers():
    src = "import json\nimport socket\nfrom urllib.request import urlopen\n"
    violations = scan_method_source(src, "methods/x.py")
    assert len(violations) == 2
    assert violations[0].startswith("methods/x.py:2: imports 'socket'")
    assert "urllib.request" in violations[1]
    assert violations[1].startswith("methods/x.py:3:")


def test_process_ffi_and_dynamic_import_are_flagged():
    for src, needle in [
        ("import subprocess\n", "subprocess"),
        ("import multiprocessing\n", "multiprocessing"),
        ("import ctypes\n", "ctypes"),
        ("import importlib\n", "importlib"),
        ("from importlib import import_module\n", "importlib"),
        ("import asyncio\n", "asyncio"),
        ("import http.client\n", "http.client"),
        ("import pickle\n", "pickle"),
    ]:
        violations = scan_method_source(src, "m.py")
        assert violations and needle in violations[0], src


def test_os_level_dangers_are_flagged():
    src = (
        "import os\n"
        "os.system('rm -rf /')\n"
        "os.execv('/bin/sh', [])\n"
        "os.fork()\n"
        "os.spawnl(os.P_NOWAIT, '/bin/sh')\n"
    )
    violations = scan_method_source(src, "m.py")
    joined = " ".join(violations)
    assert "os.system" in joined
    assert "os.execv" in joined
    assert "os.fork" in joined
    assert "os.spawnl" in joined
    # `import os` itself is allowed — os.path is legitimate for a method.
    assert not any("imports 'os'" in v for v in violations)


def test_from_os_import_of_danger_is_flagged():
    violations = scan_method_source("from os import system\n", "m.py")
    assert violations and "os.system" in violations[0]


def test_dunder_tricks_are_flagged():
    src = (
        "__import__('socket')\n"
        "eval('1+1')\n"
        "exec('x=1')\n"
        "compile('x', '<s>', 'exec')\n"
        "print(().__class__.__subclasses__())\n"
        "print(__builtins__)\n"
    )
    violations = scan_method_source(src, "m.py")
    joined = " ".join(violations)
    expected = ("__import__()", "eval()", "exec()", "compile()", "__subclasses__", "__builtins__")
    for needle in expected:
        assert needle in joined, needle


def test_syntax_error_is_a_violation():
    violations = scan_method_source("def broken(:\n", "m.py")
    assert len(violations) == 1
    assert "does not parse as Python" in violations[0]


def test_relative_import_is_flagged():
    violations = scan_method_source("from . import helper\n", "m.py")
    assert violations and "relative import" in violations[0]


def _pack(tmp_path: Path, method_source: str) -> Path:
    (tmp_path / "methods").mkdir()
    (tmp_path / "methods/evil.py").write_text(method_source)
    (tmp_path / "pack.yaml").write_text(
        "pack: scan-me\nversion: 0.1.0\ndisplay_name: Scan Me\n"
        "methods:\n"
        "  - slug: evil\n    display_name: Evil\n    entrypoint: methods/evil.py\n"
    )
    return tmp_path


def test_validate_pack_reports_scan_violations_as_errors(tmp_path):
    pack_dir = _pack(tmp_path, "import socket\nimport json\n")
    manifest, _schemas, errors = validate_pack(pack_dir)
    assert manifest is not None
    assert len(errors) == 1
    assert errors[0].startswith("method 'evil': methods/evil.py:1: imports 'socket'")


def test_validate_pack_accepts_a_clean_method(tmp_path):
    pack_dir = _pack(tmp_path, CLEAN)
    _manifest, _schemas, errors = validate_pack(pack_dir)
    assert errors == []
