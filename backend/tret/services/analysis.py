"""The Learning edition's unvetted analysis lane (R08): model-written Python,
run in a stricter sandbox than a pack method, recorded, and labelled.

Gate: cohort edition AND `TRET_UNVETTED_ANALYSIS=on` (`lane_enabled`). Nothing
here is reachable from the standard edition.

Why a stricter sandbox than methods.py: a method is operator-vetted code; this
code is written by a model, per call (web research is on, so prompt injection can
choose the code), in a process whose parent holds the deployment's secrets (DB URL,
Stripe, OpenRouter keys) next to a volume that holds other workspaces' documents.

THE BOUNDARY is bubblewrap (`bwrap`). The child runs in brand-new user, pid, net,
ipc, uts and cgroup namespaces with a brand-new root that contains only read-only
binds of the Python installation (and the system library directories it links
against): no /app, /data, /packs, /tmp, /run, /etc, no host /proc (a fresh /proc
of the new pid namespace), no network, an empty environment, all capabilities
dropped, a tmpfs working directory, and `--die-with-parent`. Code and inputs
arrive on stdin. `isolation: "bwrap"`.

FAIL CLOSED: if bwrap is missing or a probe run inside it fails, the lane refuses
to run (`isolation: "unavailable"`). The only way to run without it is the explicit
operator setting `unvetted_analysis_allow_weak_sandbox` (local development and
tests): the child then runs as a plain process under rlimits, an empty
environment and `python -I`, with NO filesystem or network isolation
(`isolation: "weak"`, said in the tool result). The weak mode is not a boundary.

Defence in depth, none of it a boundary: rlimits (CPU, address space, open files,
file size), a wall-clock timeout that kills the whole process group, a hard output
cap, a process-wide concurrency limit, and the code checks in analysis_runner.py
(AST allow-list incl. no `match`, no underscore names, positive builtins, guarded
imports handing out module snapshots).

Values from this lane are NEVER registered as retrieved values: the caller
(`engine/tools.py::run_analysis`) puts them in `ctx.unvetted_values`, so a
verdict's cited-values check cannot accept them.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
import time
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.config import get_settings
from tret.db.models import AnalysisRun, Dataset, DatasetRow
from tret.services import analysis_runner
from tret.services.edition import resolve_edition
from tret.services.methods import (
    MAX_OUTPUT_BYTES,
    MAX_OUTPUT_ROWS,
    MAX_STDERR_CHARS,
    _exchange,
)

log = logging.getLogger("tret.analysis")

validate_code = analysis_runner.validate_code

RUNNER_PATH = Path(analysis_runner.__file__).resolve()
ANALYSIS_TIMEOUT_SECONDS = 30.0
RLIMIT_CPU_SECONDS = 20
RLIMIT_ADDRESS_SPACE = 512 * 1024 * 1024
RLIMIT_OPEN_FILES = 64
RLIMIT_PROCESSES = 16
RLIMIT_FILE_SIZE = 1024 * 1024
MAX_INPUT_ROWS = 50_000
MAX_INPUT_BYTES = 20 * 1024 * 1024
NOBODY = 65534
UNVETTED_LABEL = (
    "UNVETTED CALCULATION (analysis/{id}) — not a vetted method; label these figures as unvetted."
)

LANE_UNAVAILABLE = (
    "The unvetted calculation lane is unavailable on this server: no sandbox (bubblewrap) is usable."
)
LANE_BUSY = "The unvetted calculation lane is busy; try again in a minute."
QUEUE_WAIT_SECONDS = 60.0
PROBE_TIMEOUT_SECONDS = 15.0

# Inside the sandbox.
SANDBOX_RUNNER = "/opt/tret-analysis/analysis_runner.py"
SANDBOX_WORKDIR = "/work"
# System library directories the interpreter links against, bound read-only only
# when they exist (a symlinked /lib is recreated as a symlink).
_LIB_DIRS = ("/usr/lib", "/usr/lib64", "/lib", "/lib64")


class AnalysisError(Exception):
    def __init__(self, message: str, isolation: str | None = None):
        super().__init__(message)
        self.isolation = isolation


def lane_enabled(workspace_settings: dict | None) -> bool:
    """Cohort edition AND the operator switch."""
    s = get_settings()
    return bool(s.unvetted_analysis) and resolve_edition(workspace_settings, s.edition) == "cohort"


def is_cohort(workspace_settings: dict | None) -> bool:
    return resolve_edition(workspace_settings, get_settings().edition) == "cohort"


def cohort_tool_names(workspace_settings: dict | None) -> list[str]:
    """Tools the cohort chat harness should be offered on top of the standard set
    (for workspace.py's chat-harness seeding to call)."""
    if not is_cohort(workspace_settings):
        return []
    names = ["import_table"]
    if lane_enabled(workspace_settings):
        names.append("run_analysis")
    return names


# ── isolation: bubblewrap, probed once ────────────────────────────────────────
_bwrap_path: str | None = None
_bwrap_ok: bool | None = None
_bwrap_reason = ""
_sem_state: tuple | None = None


def reset_isolation_probe() -> None:  # tests
    global _bwrap_path, _bwrap_ok, _bwrap_reason, _sem_state
    _bwrap_path, _bwrap_ok, _bwrap_reason, _sem_state = None, None, "", None


def _bind_args() -> list[str]:
    """Read-only binds for the child's new root: the interpreter's installation and
    the system library directories, nothing else. Raises AnalysisError when that
    would expose the storage directory (or when it cannot be worked out)."""
    py = Path(sys.executable).resolve()
    base = Path(sys.base_prefix).resolve()
    if base == base.parent:
        raise AnalysisError("python prefix is the filesystem root; refusing to bind it into the sandbox")
    roots: list[Path] = [base]
    if base not in py.parents:
        roots.append(py)
    symlinks: list[tuple[str, str]] = []
    for lib in _LIB_DIRS:
        p = Path(lib)
        if p.is_symlink():
            symlinks.append((os.readlink(p), lib))
        elif p.is_dir():
            r = p.resolve()
            if not any(r == x or x in r.parents for x in roots):
                roots.append(r)
    guarded = []
    storage = getattr(get_settings(), "storage_dir", None)
    if storage:
        guarded.append(Path(storage).resolve())
    guarded.append(Path("/data"))
    for r in roots:
        for g in guarded:
            if r == g or g in r.parents or r in g.parents:
                raise AnalysisError(f"python installation {r} overlaps {g}; refusing to bind it into the sandbox")
    args: list[str] = []
    for r in roots:
        args += ["--ro-bind", str(r), str(r)]
    for target, link in symlinks:
        args += ["--symlink", target, link]
    return args


def build_bwrap_command(bwrap: str, inner: list[str] | None = None) -> list[str]:
    """argv for the sandboxed child. Pure (no process is started), so its shape is
    testable on any platform. `inner` is the command run inside; the default is
    the runner script, bound read-only into the sandbox."""
    py = str(Path(sys.executable).resolve())
    argv = [
        bwrap,
        "--unshare-all",  # user, pid, net, ipc, uts, cgroup (and mount)
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--cap-drop", "ALL",
        *_bind_args(),
        "--proc", "/proc",  # the NEW pid namespace's /proc, never the host's
        "--dev", "/dev",
        "--tmpfs", SANDBOX_WORKDIR,
        "--chdir", SANDBOX_WORKDIR,
    ]
    if inner is None:
        argv += ["--ro-bind", str(RUNNER_PATH), SANDBOX_RUNNER]
        inner = [py, "-I", "-S", SANDBOX_RUNNER]
    return [*argv, "--", *inner]


def _kill_group(proc) -> None:
    """Kill the child's whole process group (it was started in its own session);
    the bwrap init dying takes its pid namespace with it."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _preexec(drop_privileges: bool, limit_processes: bool):
    import resource

    def run() -> None:
        limits = [
            ("RLIMIT_CPU", RLIMIT_CPU_SECONDS),
            ("RLIMIT_AS", RLIMIT_ADDRESS_SPACE),
            ("RLIMIT_NOFILE", RLIMIT_OPEN_FILES),
            ("RLIMIT_FSIZE", RLIMIT_FILE_SIZE),
            ("RLIMIT_CORE", 0),
        ]
        # RLIMIT_NPROC counts every process of the uid, the whole app included, so
        # it is only used for the weak dev mode; under bwrap the pid namespace and
        # the absence of any way to fork (no os/subprocess/threading) cover it.
        if limit_processes:
            limits.append(("RLIMIT_NPROC", RLIMIT_PROCESSES))
        for name, value in limits:
            lim = getattr(resource, name, None)
            if lim is None:
                continue
            try:
                resource.setrlimit(lim, (value, value))
            except (ValueError, OSError):
                pass  # e.g. RLIMIT_AS is a no-op on macOS
        if drop_privileges:
            os.setgroups([])
            os.setgid(NOBODY)
            os.setuid(NOBODY)

    return run


def _should_drop() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


async def _probe_bwrap() -> bool:
    """Run a tiny script through the exact sandbox command and check the walls hold:
    host dirs absent, no network. Any failure means the lane is unavailable."""
    global _bwrap_path, _bwrap_reason
    if sys.platform != "linux":
        _bwrap_reason = f"bubblewrap is Linux-only (platform is {sys.platform})"
        return False
    path = shutil.which("bwrap")
    if not path:
        _bwrap_reason = "bwrap is not on PATH"
        return False
    code = (
        "import os, socket\n"
        "leak = [p for p in ('/data', '/app', '/etc/passwd', '/tmp', '/run') if os.path.exists(p)]\n"
        "s = socket.socket(); s.settimeout(2)\n"
        "try:\n    s.connect(('1.1.1.1', 53)); leak.append('network')\n"
        "except OSError:\n    pass\n"
        "print(1 if not leak else 'LEAK ' + ','.join(leak))\n"
    )
    try:
        argv = build_bwrap_command(path, [str(Path(sys.executable).resolve()), "-I", "-S", "-c", code])
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd="/", env={}, close_fds=True, start_new_session=True,
            preexec_fn=_preexec(_should_drop(), False),
        )
    except (OSError, AnalysisError) as e:
        _bwrap_reason = f"could not start bwrap: {e}"
        return False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=PROBE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        _kill_group(proc)
        _bwrap_reason = "the bwrap probe timed out"
        return False
    finally:
        if proc.returncode is None:
            _kill_group(proc)
    if proc.returncode != 0 or out.strip() != b"1":
        _bwrap_reason = (
            f"the bwrap probe failed (exit {proc.returncode}): "
            f"{(out + err).decode(errors='replace').strip()[:300]}"
        )
        return False
    _bwrap_path = path
    return True


async def isolation_mode() -> str:
    """"bwrap" when the sandbox probe passes (probed once per process), else "weak"
    only if the operator explicitly allowed it, else "unavailable" (fail closed)."""
    global _bwrap_ok
    if _bwrap_ok is None:
        _bwrap_ok = await _probe_bwrap()
    if _bwrap_ok:
        return "bwrap"
    settings = get_settings()
    if settings.unvetted_analysis_allow_weak_sandbox:
        if str(settings.environment).strip().lower() in ("production", "prod"):
            log.warning(
                "TRET_UNVETTED_ANALYSIS_ALLOW_WEAK_SANDBOX is ignored in production: "
                "the run_analysis lane stays unavailable without a real sandbox."
            )
            return "unavailable"
        return "weak"
    return "unavailable"


async def log_lane_status() -> None:
    """Once at startup, when the lane is enabled: say which isolation it will use."""
    if not get_settings().unvetted_analysis:
        return
    mode = await isolation_mode()
    if mode == "bwrap":
        log.info("run_analysis lane enabled: sandbox is bubblewrap (%s)", _bwrap_path)
    elif mode == "weak":
        log.warning(
            "run_analysis lane enabled with the WEAK sandbox (TRET_UNVETTED_ANALYSIS_ALLOW_WEAK_SANDBOX): "
            "model-written code runs with no filesystem or network isolation. Development only. (%s)",
            _bwrap_reason,
        )
    else:
        log.error("run_analysis lane enabled but UNAVAILABLE, calls will be refused: %s", _bwrap_reason)


def _semaphore() -> asyncio.Semaphore:
    """Process-wide concurrency limit (one per event loop, rebuilt if the setting changes)."""
    global _sem_state
    loop = asyncio.get_running_loop()
    n = max(1, int(get_settings().unvetted_analysis_max_concurrent))
    if _sem_state is None or _sem_state[0] is not loop or _sem_state[1] != n:
        _sem_state = (loop, n, asyncio.Semaphore(n))
    return _sem_state[2]


# ── running ───────────────────────────────────────────────────────────────────
async def run_sandboxed(code: str, inputs: dict[str, list[dict]]) -> tuple[list[dict], str]:
    """Execute `code` against `inputs`; returns (rows, isolation). Raises AnalysisError."""
    problems = validate_code(code)
    if problems:
        raise AnalysisError("Code rejected before running:\n- " + "\n- ".join(problems[:20]))
    payload = json.dumps({"code": code, "inputs": inputs}, default=str).encode()
    if len(payload) > MAX_INPUT_BYTES:
        raise AnalysisError(
            f"Inputs are {len(payload)} bytes; the limit is {MAX_INPUT_BYTES}. Filter or aggregate first."
        )
    mode = await isolation_mode()
    if mode == "unavailable":
        raise AnalysisError(LANE_UNAVAILABLE, isolation="unavailable")
    sem = _semaphore()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=QUEUE_WAIT_SECONDS)
    except asyncio.TimeoutError:
        raise AnalysisError(LANE_BUSY, isolation=mode)
    try:
        return await _run_child(payload, mode), mode
    except AnalysisError as e:
        e.isolation = e.isolation or mode
        raise
    finally:
        sem.release()


async def _run_child(payload: bytes, mode: str) -> list[dict]:
    drop = _should_drop()
    workdir = tempfile.mkdtemp(prefix="tret-analysis-") if mode == "weak" else None
    proc = None
    try:
        if mode == "bwrap":
            argv, cwd = build_bwrap_command(_bwrap_path or "bwrap"), "/"
        else:
            if drop:
                os.chown(workdir, NOBODY, NOBODY)
            os.chmod(workdir, 0o700)
            argv, cwd = [sys.executable, "-I", str(RUNNER_PATH)], workdir
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env={},
            close_fds=True,
            start_new_session=True,  # own process group, killed as a whole
            preexec_fn=_preexec(drop, mode == "weak"),
        )
        try:
            stdout, stderr, overflowed = await asyncio.wait_for(
                _exchange(proc, payload), timeout=ANALYSIS_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            _kill_group(proc)
            raise AnalysisError(f"Analysis timed out after {ANALYSIS_TIMEOUT_SECONDS:.0f}s")
        if overflowed:
            raise AnalysisError(f"Analysis output exceeds the size cap ({MAX_OUTPUT_BYTES} bytes)")
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace")[:MAX_STDERR_CHARS]
            raise AnalysisError(f"Analysis exited {proc.returncode}: {detail}")
        return _parse_output(stdout.decode(errors="replace"))
    finally:
        if proc is not None and proc.returncode is None:
            _kill_group(proc)  # also reaps anything a failed or overflowed run left behind
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def _parse_output(text: str) -> list[dict]:
    """The code prints its result; the last non-empty line that parses as the
    contract wins, so debugging prints before it do not break the run."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise AnalysisError("The code printed nothing; it must print json.dumps({'rows': [...]})")
    try:
        result = json.loads(lines[-1])
        rows = result["rows"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise AnalysisError(f"The last line printed is not valid {{'rows': [...]}} JSON: {e}")
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise AnalysisError("'rows' must be a list of objects (one dict per row)")
    if len(rows) > MAX_OUTPUT_ROWS:
        raise AnalysisError(f"Analysis returned {len(rows)} rows (cap {MAX_OUTPUT_ROWS})")
    return rows


def _rows_hash(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


async def load_dataset_rows(db: AsyncSession, project_id: uuid.UUID, name: str) -> tuple[list[dict], dict]:
    ds = (
        await db.execute(select(Dataset).where(Dataset.project_id == project_id, Dataset.name == name))
    ).scalar_one_or_none()
    if ds is None:
        names = (await db.execute(select(Dataset.name).where(Dataset.project_id == project_id))).scalars().all()
        raise AnalysisError(f"Dataset '{name}' not found. Available: {sorted(set(names))}")
    if ds.row_count > MAX_INPUT_ROWS:
        raise AnalysisError(f"Dataset '{name}' has {ds.row_count} rows; the limit is {MAX_INPUT_ROWS}.")
    rows = [
        r.data
        for r in (
            await db.execute(
                select(DatasetRow).where(DatasetRow.dataset_id == ds.id).order_by(DatasetRow.row_index)
            )
        ).scalars()
    ]
    return rows, {"kind": "dataset", "ref": name, "document_id": str(ds.document_id) if ds.document_id else None}


def describe_input(name: str, rows: list[dict], meta: dict) -> dict:
    return {"name": name, "kind": meta.get("kind", "dataset"), "ref": meta.get("ref", ""),
            "sha256": _rows_hash(rows), "rows": len(rows),
            **({"document_id": meta["document_id"]} if meta.get("document_id") else {})}


async def execute_analysis(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID | None,
    purpose: str,
    code: str,
    inputs: dict[str, tuple[list[dict], dict]],
) -> AnalysisRun:
    """Run, persist an AnalysisRun (also when it fails), and return it. A failed
    run is returned with status 'failed' and its error; the caller decides how to
    surface it."""
    record = AnalysisRun(
        project_id=project_id,
        run_id=run_id,
        purpose=(purpose or "")[:2000],
        code=code,
        code_sha=hashlib.sha256((code or "").encode()).hexdigest(),
        inputs=[describe_input(n, rows, meta) for n, (rows, meta) in inputs.items()],
    )
    start = time.monotonic()
    isolation = "not_run"
    try:
        rows, isolation = await run_sandboxed(code, {n: rows for n, (rows, _m) in inputs.items()})
        record.output = rows
        record.output_hash = _rows_hash(rows)
        record.row_count = len(rows)
        record.status = "completed"
    except AnalysisError as e:
        record.status = "failed"
        record.error = str(e)
        isolation = e.isolation or "not_run"
    finally:
        record.duration_ms = int((time.monotonic() - start) * 1000)
    record.inputs = [
        *record.inputs,
        {"name": "_sandbox", "kind": "sandbox", "ref": "", "sha256": "", "rows": 0, "isolation": isolation},
    ]
    db.add(record)
    await db.flush()
    return record
