"""R08 unvetted lane: run_analysis sandbox, gating, recording, labelling; and the
cohort-only prompt block (R01/R02/R09/R12/R14)."""
from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import ARRAY, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from tret.config import get_settings
from tret.db.models import AnalysisRun, Base, Dataset, DatasetRow, Project, Workspace
from tret.engine.context import assemble_context
from tret.engine.tools import RunContext, ToolError, run_analysis
from tret.services import analysis as analysis_service
from tret.services.analysis import validate_code


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # pragma: no cover
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_on_sqlite(type_, compiler, **kw):  # pragma: no cover
    return "JSON"


@pytest.fixture()
def lane_on(monkeypatch):
    monkeypatch.setenv("TRET_UNVETTED_ANALYSIS", "true")
    # Without bwrap (macOS, most CI) the lane refuses unless this explicit dev opt-out is set.
    monkeypatch.setenv("TRET_UNVETTED_ANALYSIS_ALLOW_WEAK_SANDBOX", "true")
    get_settings.cache_clear()
    analysis_service.reset_isolation_probe()
    yield
    get_settings.cache_clear()
    analysis_service.reset_isolation_probe()


@pytest.fixture()
async def world(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'an.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        ws = Workspace(name="W", settings={"edition": "cohort"})
        db.add(ws)
        await db.flush()
        project = Project(workspace_id=ws.id, name="P")
        db.add(project)
        await db.flush()
        ds = Dataset(project_id=project.id, name="t", schema_json={"columns": ["a"]}, row_count=2)
        db.add(ds)
        await db.flush()
        db.add(DatasetRow(dataset_id=ds.id, row_index=0, data={"a": "2"}))
        db.add(DatasetRow(dataset_id=ds.id, row_index=1, data={"a": "3"}))
        await db.commit()
        ctx = RunContext(db=db, run_id=uuid.uuid4(), project_id=project.id, pack_id=None, doctrine_sha=None,
                         model_used=None, document_ids=[], output_schemas={}, workspace_id=ws.id)
        yield ctx, ws
    await engine.dispose()


GOOD = (
    "import json\n"
    "total = sum(float(r['a']) for r in inputs['t'])\n"
    "print(json.dumps({'rows': [{'total': total}]}))\n"
)


async def test_happy_path_records_run_and_keeps_values_unvetted(world, lane_on):
    ctx, _ = world
    out = await run_analysis(ctx, purpose="sum a", code=GOOD, inputs={"t": "t"})
    run = (await ctx.db.execute(select(AnalysisRun))).scalars().one()
    assert out.startswith(f"UNVETTED CALCULATION (analysis/{run.id}) — not a vetted method; label these figures as unvetted.")
    body = json.loads(out.split("\n", 1)[1])
    assert body["rows"][0]["total"] == 5.0 and body["rows"][0]["_row"] == f"analysis/{run.id}:0"
    assert body["sandbox_isolation"] in ("bwrap", "weak")
    assert run.status == "completed" and run.code == GOOD and run.output == [{"total": 5.0}]
    assert run.code_sha and run.output_hash and run.row_count == 1 and run.purpose == "sum a"
    assert run.inputs[0]["name"] == "t" and run.inputs[0]["rows"] == 2 and run.inputs[0]["sha256"]
    assert any(i.get("kind") == "sandbox" for i in run.inputs)
    assert ctx.retrieved_values == []
    assert ctx.unvetted_values == [{"dataset": f"analysis/{run.id}", "row_ref": f"analysis/{run.id}:0",
                                    "column": "total", "value": "5.0"}]
    assert ctx.analysis_run_ids == [str(run.id)]


async def test_gated_off_by_setting_and_outside_cohort(world, monkeypatch):
    ctx, ws = world
    get_settings.cache_clear()
    with pytest.raises(ToolError, match="not available"):  # setting off (default)
        await run_analysis(ctx, purpose="x", code=GOOD, inputs={"t": "t"})
    monkeypatch.setenv("TRET_UNVETTED_ANALYSIS", "true")
    get_settings.cache_clear()
    ws.settings = {"edition": "standard"}
    await ctx.db.flush()
    with pytest.raises(ToolError, match="not available"):
        await run_analysis(ctx, purpose="x", code=GOOD, inputs={"t": "t"})
    assert (await ctx.db.execute(select(AnalysisRun))).scalars().all() == []
    get_settings.cache_clear()


ESCAPES = [
    "print(open('/proc/1/environ').read())",
    "__import__('os')",
    "x = ().__class__.__bases__",
    "getattr(1, 'real')",
    "import os",
    "import subprocess",
    "import socket",
    "import ctypes",
    "import pathlib",
    "import io",
    "import sys",
    "import builtins",
    "from os import path",
    "from . import x",
    "exec('1')",
    "eval('1')",
    "compile('1', 'f', 'exec')",
    "print('{0.__class__}'.format(1))",
    "print('{0.__class__}'.format_map({}))",
    "f = lambda a=().__class__: a",
    "g = (i for i in [1])\nprint(g.gi_frame.f_globals)",
    "def f():\n    pass\nf.func_globals",
    "x = type(1)",
    "import json\njson.__dict__",
    "import json\njson._default_encoder",
    "setattr(1, 'a', 2)",
    "vars()",
    "globals()",
    "input()",
    "breakpoint()",
    "class A(metaclass=type): pass",
    "x = 1\nx.__class__",
    "def _f(): pass",
    "import math as _m",
    # `match` patterns read attributes with a real getattr (reviewer-confirmed escapes)
    (
        "import functools\n"
        "def h(): pass\n"
        "class H: pass\n"
        "functools.update_wrapper(h, functools.update_wrapper, assigned=('__globals__',), updated=())\n"
        "match h:\n"
        "    case H(__globals__=g):\n"
        "        f = g['__builtins__']['open']('/etc/hosts')\n"
    ),
    (
        "class A: pass\n"
        "match A():\n"
        "    case A(__reduce_ex__=red):\n"
        "        parts = red(0)\n"
        "fn, o = parts[0], parts[1][1]\n"
        "match fn:\n"
        "    case o(__globals__=g):\n"
        "        os = g['__builtins__']['__import__']('os')\n"
    ),
    "x = 1\nmatch x:\n    case 1:\n        pass",
    "x = [1, 2]\nmatch x:\n    case [a, b]:\n        pass",
    "x = {'a': 1}\nmatch x:\n    case {'a': v}:\n        pass",
    "x = 1\nmatch x:\n    case int(real=r):\n        pass",
    "x = 1\nmatch x:\n    case 1 | 2 as y:\n        pass",
    "import functools\nfunctools.update_wrapper(len, len)",
    "import functools\n@functools.wraps(len)\ndef f(): pass",
    "from functools import wraps",
    "from functools import update_wrapper as u",
    "from functools import partialmethod",
    "import functools\nfunctools.singledispatch(len)",
    "def f():\n    global x\n    x = 1",
    "def f():\n    y = 1\n    def g():\n        nonlocal y\n    return g",
    "type X = int",
    "def f[_T](a): pass",
]


@pytest.mark.parametrize("code", ESCAPES)
def test_static_check_refuses_escape(code):
    assert validate_code(code), code


@pytest.mark.parametrize("code", ESCAPES)
async def test_escape_never_runs(world, lane_on, code):
    ctx, _ = world
    with pytest.raises(ToolError, match="rejected"):
        await run_analysis(ctx, purpose="x", code=code, inputs={"t": "t"})
    run = (await ctx.db.execute(select(AnalysisRun))).scalars().one()
    assert run.status == "failed" and run.output == []
    assert ctx.unvetted_values == [] and ctx.retrieved_values == []


def test_allowed_code_passes_static_check():
    assert not validate_code(
        "import json, math\nfrom collections import defaultdict\nfrom decimal import Decimal\n"
        "d = defaultdict(float)\nfor r in inputs['t']:\n    d[r['a']] += math.sqrt(4)\n"
        "print(json.dumps({'rows': [{'k': k, 'v': v} for k, v in d.items()]}))"
    )
    assert validate_code("")
    assert validate_code("x = " + "1" * 30000)


async def _run(code, inputs=None):
    return await analysis_service.run_sandboxed(code, inputs or {})


async def test_runtime_guards_hold_even_if_static_check_were_bypassed(lane_on):
    """The child re-checks, and its builtins/imports are minimal."""
    from tret.services import analysis_runner

    # child-side check: feed the runner directly, skipping the parent's validation
    import asyncio

    for code in ("import os", "print(open('/etc/passwd').read())", "x = ().__class__"):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(analysis_service.RUNNER_PATH),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={},
        )
        out, err = await proc.communicate(json.dumps({"code": code, "inputs": {}}).encode())
        assert proc.returncode == 3 and b"rejected" in err and out == b""
    # runtime layer on its own: no open / getattr builtins, module snapshots carry no modules
    ns = {"__builtins__": analysis_runner._safe_builtins(__import__("builtins"), analysis_runner._make_import(__import__, {}))}
    for src in ("open", "getattr", "type", "exec"):
        with pytest.raises(NameError):
            exec(src, ns)  # noqa: S102
    exec("import json\ntry:\n    json.codecs\n    has = True\nexcept AttributeError:\n    has = False", ns)  # noqa: S102
    assert ns["has"] is False
    with pytest.raises(ImportError):
        exec("import os", ns)  # noqa: S102


async def test_resource_abuse_is_contained(world, lane_on, monkeypatch):
    ctx, _ = world
    monkeypatch.setattr(analysis_service, "ANALYSIS_TIMEOUT_SECONDS", 3.0)
    with pytest.raises(ToolError, match="timed out|exited"):
        await run_analysis(ctx, purpose="loop", code="while True:\n    pass\n", inputs={"t": "t"})
    with pytest.raises(ToolError, match="size cap|exited|timed out"):
        await run_analysis(ctx, purpose="big", code="while True:\n    print('x' * 100000)\n", inputs={"t": "t"})
    runs = (await ctx.db.execute(select(AnalysisRun))).scalars().all()
    assert len(runs) == 2 and all(r.status == "failed" for r in runs)


async def test_bad_outputs_and_inputs(world, lane_on):
    ctx, _ = world
    with pytest.raises(ToolError, match="printed nothing"):
        await run_analysis(ctx, purpose="x", code="x = 1", inputs={"t": "t"})
    with pytest.raises(ToolError, match="not valid"):
        await run_analysis(ctx, purpose="x", code="print('hi')", inputs={"t": "t"})
    with pytest.raises(ToolError, match="not found"):
        await run_analysis(ctx, purpose="x", code=GOOD, inputs={"t": "missing"})
    with pytest.raises(ToolError, match="identifier"):
        await run_analysis(ctx, purpose="x", code=GOOD, inputs={"_t": "t"})
    ok = await run_analysis(ctx, purpose="x", code="print('debug')\n" + GOOD, inputs={"t": "t"})
    assert '"total": 5.0' in ok  # last line wins


async def test_memory_bomb(world, lane_on):
    if sys.platform != "linux":
        pytest.skip("RLIMIT_AS is not enforced on macOS")
    ctx, _ = world
    with pytest.raises(ToolError, match="exited"):
        await run_analysis(ctx, purpose="x", code="x = [0] * (10 ** 10)", inputs={"t": "t"})


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="uid drop needs root")
async def test_runs_as_nobody_when_root(world, lane_on):  # pragma: no cover
    ctx, _ = world
    assert await run_analysis(ctx, purpose="x", code=GOOD, inputs={"t": "t"})


def test_functools_attribute_copiers_are_not_in_the_runtime_snapshot():
    import functools

    from tret.services import analysis_runner

    ns = analysis_runner._snapshot(functools)
    for name in ("update_wrapper", "wraps", "partialmethod", "singledispatch", "cached_property"):
        assert not hasattr(ns, name), name
    assert hasattr(ns, "reduce") and hasattr(ns, "partial")


def test_bwrap_command_shape_has_no_host_filesystem():
    argv = analysis_service.build_bwrap_command("/usr/bin/bwrap")
    assert argv[0] == "/usr/bin/bwrap"
    for flag in ("--unshare-all", "--die-with-parent", "--new-session", "--clearenv", "--proc", "--dev", "--tmpfs", "--chdir"):
        assert flag in argv, flag
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--proc") + 1] == "/proc" and argv[argv.index("--dev") + 1] == "/dev"
    binds = [argv[i + 1] for i, a in enumerate(argv) if a == "--ro-bind"]
    assert str(analysis_service.RUNNER_PATH) in binds
    host_dirs = ("/app", "/data", "/packs", "/tmp", "/run", "/proc", "/etc", "/.fly", "/")
    assert not any(b in host_dirs for b in binds), binds
    assert "--bind" not in argv and "--bind-try" not in argv
    assert argv[argv.index("--") + 1:][-1] == analysis_service.SANDBOX_RUNNER


def test_bwrap_refuses_to_bind_the_storage_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TRET_STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path.parent))
    try:
        with pytest.raises(analysis_service.AnalysisError, match="overlaps"):
            analysis_service.build_bwrap_command("/usr/bin/bwrap")
    finally:
        get_settings.cache_clear()


@pytest.fixture()
def no_bwrap(monkeypatch):
    analysis_service.reset_isolation_probe()
    monkeypatch.setattr(analysis_service.shutil, "which", lambda *_a, **_k: None)
    yield
    analysis_service.reset_isolation_probe()


async def test_lane_refuses_without_bwrap_and_without_the_weak_setting(world, no_bwrap, monkeypatch):
    ctx, _ = world
    monkeypatch.setenv("TRET_UNVETTED_ANALYSIS", "true")
    monkeypatch.delenv("TRET_UNVETTED_ANALYSIS_ALLOW_WEAK_SANDBOX", raising=False)
    get_settings.cache_clear()
    try:
        assert await analysis_service.isolation_mode() == "unavailable"
        with pytest.raises(ToolError, match=r"unavailable on this server: no sandbox \(bubblewrap\) is usable"):
            await run_analysis(ctx, purpose="x", code=GOOD, inputs={"t": "t"})
        run = (await ctx.db.execute(select(AnalysisRun))).scalars().one()
        assert run.status == "failed" and run.output == []
        assert next(i for i in run.inputs if i.get("kind") == "sandbox")["isolation"] == "unavailable"
        assert ctx.unvetted_values == []
    finally:
        get_settings.cache_clear()


async def test_weak_mode_is_explicit_and_said_in_the_result(world, no_bwrap, monkeypatch):
    ctx, _ = world
    monkeypatch.setenv("TRET_UNVETTED_ANALYSIS", "true")
    monkeypatch.setenv("TRET_UNVETTED_ANALYSIS_ALLOW_WEAK_SANDBOX", "true")
    get_settings.cache_clear()
    try:
        out = await run_analysis(ctx, purpose="x", code=GOOD, inputs={"t": "t"})
        body = json.loads(out.split("\n", 1)[1])
        assert body["sandbox_isolation"] == "weak" and "WEAK SANDBOX" in body["note"]
    finally:
        get_settings.cache_clear()


async def test_busy_lane_is_refused_after_the_wait(lane_on, monkeypatch):
    monkeypatch.setattr(analysis_service, "QUEUE_WAIT_SECONDS", 0.2)
    monkeypatch.setenv("TRET_UNVETTED_ANALYSIS_MAX_CONCURRENT", "1")
    get_settings.cache_clear()
    sem = analysis_service._semaphore()
    await sem.acquire()
    try:
        with pytest.raises(analysis_service.AnalysisError, match="busy"):
            await analysis_service.run_sandboxed(GOOD, {"t": [{"a": "1"}]})
    finally:
        sem.release()


# ── real bubblewrap (Linux with bwrap on PATH only) ──────────────────────────
needs_bwrap = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bwrap") is None, reason="needs Linux and bubblewrap"
)


async def _in_sandbox(code: str) -> tuple[int, str]:
    """Run plain Python `code` inside the lane's exact bwrap command (the runner's
    own checks would forbid `open`, so the boundary is probed from below)."""
    import asyncio

    analysis_service.reset_isolation_probe()
    if not await analysis_service._probe_bwrap():
        pytest.skip(f"bwrap is present but unusable here: {analysis_service._bwrap_reason}")
    argv = analysis_service.build_bwrap_command(
        analysis_service._bwrap_path, [str(Path(sys.executable).resolve()), "-I", "-S", "-c", code]
    )
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env={}, start_new_session=True,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode()


@needs_bwrap
async def test_bwrap_child_has_no_host_files():
    rc, out = await _in_sandbox(
        "import os\n"
        "print(sorted(p for p in ('/etc/hosts', '/etc/passwd', '/data', '/app', '/tmp', '/run', '/.fly', '/packs') "
        "if os.path.exists(p)))\n"
        "print(sorted(os.listdir('/')))"
    )
    assert rc == 0 and out.splitlines()[0] == "[]", out
    assert "etc" not in out.splitlines()[1] and "data" not in out.splitlines()[1]


@needs_bwrap
async def test_bwrap_child_sees_its_own_proc_not_the_hosts(monkeypatch):
    monkeypatch.setenv("TRET_TEST_SECRET", "s3cret-value")
    rc, out = await _in_sandbox(
        f"import os\n"
        f"pids = [p for p in os.listdir('/proc') if p.isdigit()]\n"
        f"print({os.getpid()} in map(int, pids), len(pids))\n"
        f"print('s3cret-value' in open('/proc/1/environ').read())"
    )
    lines = out.splitlines()
    assert rc == 0, out
    assert lines[0].startswith("False"), out  # the host pid of this test does not exist there
    assert lines[1] == "False"


@needs_bwrap
async def test_bwrap_child_has_no_network():
    rc, out = await _in_sandbox(
        "import socket\n"
        "s = socket.socket(); s.settimeout(2)\n"
        "try:\n    s.connect(('1.1.1.1', 53)); print('connected')\n"
        "except OSError as e:\n    print('blocked')\n"
    )
    assert rc == 0 and out.strip() == "blocked", out


@needs_bwrap
async def test_bwrap_timeout_kills_the_whole_tree(lane_on, monkeypatch):
    import asyncio

    analysis_service.reset_isolation_probe()
    if not await analysis_service._probe_bwrap():
        pytest.skip("bwrap is present but unusable here")
    analysis_service._bwrap_ok = True
    monkeypatch.setattr(analysis_service, "ANALYSIS_TIMEOUT_SECONDS", 2.0)
    with pytest.raises(analysis_service.AnalysisError, match="timed out"):
        await analysis_service.run_sandboxed("while True:\n    pass\n", {})
    pgrep = shutil.which("pgrep")
    if pgrep is None:
        pytest.skip("pgrep not available")
    for _ in range(50):
        proc = await asyncio.create_subprocess_exec(
            pgrep, "-f", analysis_service.SANDBOX_RUNNER, stdout=asyncio.subprocess.DEVNULL
        )
        if await proc.wait() == 1:  # nothing matches: no orphan left behind
            return
        await asyncio.sleep(0.1)
    pytest.fail("the sandboxed child outlived the timeout")


@needs_bwrap
async def test_bwrap_runs_the_lane_end_to_end(lane_on):
    rows, mode = await analysis_service.run_sandboxed(GOOD, {"t": [{"a": "1"}, {"a": "4"}]})
    if mode != "bwrap":
        pytest.skip("bwrap unusable here")
    assert rows == [{"total": 5.0}]


def test_lane_and_tool_gating_helpers(monkeypatch):
    monkeypatch.setenv("TRET_UNVETTED_ANALYSIS", "true")
    get_settings.cache_clear()
    assert analysis_service.cohort_tool_names({"edition": "cohort"}) == ["import_table", "run_analysis"]
    assert analysis_service.cohort_tool_names({"edition": "standard"}) == []
    monkeypatch.setenv("TRET_UNVETTED_ANALYSIS", "false")
    get_settings.cache_clear()
    assert analysis_service.cohort_tool_names({"edition": "cohort"}) == ["import_table"]
    assert not analysis_service.lane_enabled({"edition": "cohort"})
    get_settings.cache_clear()


# ── prompt ────────────────────────────────────────────────────────────────────
def _system(**kw) -> str:
    from types import SimpleNamespace

    harness = SimpleNamespace(system_prompt_extra=None, name="h")
    return assemble_context(harness, None, "chat", {}, **kw).system


def test_cohort_prompt_block_only_in_cohort():
    std = _system()
    assert "Learning edition" not in std and "run_analysis" not in std
    cohort = _system(cohort=True)
    for needle in ("facility_inventory", "illustrative", "propose_connected_write", "draft_file", "_brand", "raw=true"):
        assert needle in cohort, needle
    assert "run_analysis" not in cohort
    both = _system(cohort=True, unvetted_analysis=True)
    assert "unvetted" in both and "run_analysis" in both
    assert "run_analysis" not in _system(cohort=False, unvetted_analysis=True)
