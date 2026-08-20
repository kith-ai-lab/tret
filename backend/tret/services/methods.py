"""Deterministic method execution — the compute side of the deterministic lane.

Methods are pack-authored Python scripts (operator-vetted, versioned with the
pack). The agent may invoke them with parameters; it never writes code. Each
execution is manifest-pinned: params, code sha, input summary, output hash.

Contract with the script:
  stdin:  {"params": {...}, "inputs": {"<name>": [row, ...]}}
  stdout: {"rows": [{...}, ...]}   (flat dicts; stdlib only; pure function)

What the sandbox actually guarantees
-----------------------------------
Every method runs as a separate short-lived process:

  * `python -I` — isolated interpreter: no PYTHON* env vars, no user
    site-packages, cwd not on sys.path.
  * empty environment (no API keys, no DB URL), inherited fds closed;
    stdin/stdout are the whole contract (stderr is captured for diagnostics
    only and truncated).
  * rlimits: CPU 30s, address space 768MB, 64 open files, low process count —
    each applied only where the platform supports it.
  * wall-clock timeout from the manifest, plus output size and row caps. The
    size cap is applied *as stdout streams*: a method that over-produces is
    killed at the cap rather than buffered whole and rejected afterwards.
  * no DB handle — inputs are materialized by the runner and passed on stdin.
  * pack integrity is re-verified before execution: a pack edited since install
    fails loudly rather than producing untrusted numbers.
  * on Linux with the `unshare` binary and permission to use it, the process
    runs in an empty network namespace (`TRET_METHODS_NETWORK_ISOLATION`,
    default on). This is the only real network control.

What it does NOT guarantee: filesystem isolation. A method runs as the tret
user and can read anything that user can read (including ./storage and the
pack tree) and write anywhere that user can write. Off Linux — or without
`unshare` — it can also open sockets; the AST scan in `tret/packs/safety.py`
is a deterrent there, not a boundary. Pack code is therefore operator-trusted:
installing a pack is deploying code. Real isolation means running tret (or at
least this subprocess) in a container/VM whose filesystem and network you
control — see docs/hardening.md.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import resource
import shutil
import sys
import time
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.config import get_settings
from tret.db.models import Dataset, DatasetRow, Finding, MethodRun, Pack
from tret.packs.integrity import PackIntegrityError, verify_pack_integrity

log = logging.getLogger("tret.methods")

MAX_OUTPUT_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_ROWS = 2000
MAX_STDERR_CHARS = 2000
# What we are willing to hold in memory from the child's stderr. Diagnostics are
# truncated to MAX_STDERR_CHARS anyway; the rest is drained and dropped.
MAX_STDERR_BYTES = 16 * 1024
READ_CHUNK_BYTES = 64 * 1024
RLIMIT_CPU_SECONDS = 30
RLIMIT_ADDRESS_SPACE = 768 * 1024 * 1024
RLIMIT_OPEN_FILES = 64  # stdin/stdout/stderr + imports; no room for socket farms
RLIMIT_PROCESSES = 16  # blocks fork bombs; per-uid, so keep it above zero


class MethodError(Exception):
    pass


def _set_limits() -> None:  # runs in the child before exec
    resource.setrlimit(resource.RLIMIT_CPU, (RLIMIT_CPU_SECONDS, RLIMIT_CPU_SECONDS))
    for name, value in (
        ("RLIMIT_AS", RLIMIT_ADDRESS_SPACE),
        ("RLIMIT_NOFILE", RLIMIT_OPEN_FILES),
        ("RLIMIT_NPROC", RLIMIT_PROCESSES),
    ):
        limit = getattr(resource, name, None)
        if limit is None:
            continue  # not all rlimits exist on all platforms
        try:
            resource.setrlimit(limit, (value, value))
        except (ValueError, OSError):
            pass  # e.g. RLIMIT_AS is a no-op/unsupported on macOS


# None = not probed yet; [] = isolation unavailable; [...] = command prefix.
_isolation_prefix: list[str] | None = None


async def network_isolation_prefix() -> list[str]:
    """Command prefix that drops the child into an empty network namespace.

    Linux + `unshare` only, and only if we may actually create the namespace —
    probed once per process, with a logged warning on fallback so an operator
    who asked for isolation learns they did not get it.
    """
    global _isolation_prefix
    if not get_settings().methods_network_isolation:
        return []
    if _isolation_prefix is not None:
        return _isolation_prefix

    _isolation_prefix = []
    if sys.platform != "linux":
        log.warning(
            "TRET_METHODS_NETWORK_ISOLATION is on but this is %s, not Linux: methods run "
            "WITHOUT network isolation (fine for development; not for production)",
            sys.platform,
        )
        return _isolation_prefix
    unshare = shutil.which("unshare")
    if unshare is None:
        log.warning(
            "TRET_METHODS_NETWORK_ISOLATION is on but `unshare` is not installed: methods "
            "run WITHOUT network isolation (install util-linux)"
        )
        return _isolation_prefix
    try:
        probe = await asyncio.create_subprocess_exec(
            unshare,
            "--net",
            "--",
            "true",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(probe.communicate(), timeout=10)
        ok = probe.returncode == 0
    except (OSError, asyncio.TimeoutError) as e:
        ok, err = False, str(e).encode()
    if ok:
        _isolation_prefix = [unshare, "--net", "--"]
        log.info("method sandbox: network isolation enabled via unshare --net")
    else:
        log.warning(
            "TRET_METHODS_NETWORK_ISOLATION is on but `unshare --net` is not permitted here "
            "(%s): methods run WITHOUT network isolation. Grant CAP_SYS_ADMIN / enable "
            "unprivileged user namespaces, or isolate the container's network instead",
            err.decode(errors="replace").strip()[:200] or "no detail",
        )
    return _isolation_prefix


# ── bounded I/O with the child ────────────────────────────────────────────────
# `proc.communicate()` buffers the whole of stdout before returning, so the
# output-size cap could only be applied to something already in memory: a method
# that printed gigabytes took the process down with it instead of being capped —
# the one failure mode a cap exists to prevent. Reading in bounded chunks costs
# at most one chunk of overshoot and lets us kill the writer the moment it goes
# over.
async def _feed_stdin(proc, payload: bytes) -> None:
    """Write the method's stdin and close it, tolerating a child that never reads."""
    try:
        proc.stdin.write(payload)
        await proc.stdin.drain()
    except OSError:
        pass  # the method exited (or was killed) without consuming its input
    finally:
        try:
            proc.stdin.close()
        except OSError:  # pragma: no cover - transport already gone
            pass


async def _read_capped(stream, limit: int) -> tuple[bytes, bool]:
    """Read up to `limit` bytes. Returns (data, overflowed) and stops at overflow."""
    buf = bytearray()
    while len(buf) <= limit:
        chunk = await stream.read(READ_CHUNK_BYTES)
        if not chunk:
            return bytes(buf), False
        buf.extend(chunk)
    return bytes(buf), True


async def _drain_capped(stream, keep: int) -> bytes:
    """Read to EOF, keeping only the first `keep` bytes.

    Unlike stdout this keeps reading past the cap and throws the rest away: a
    pipe nobody drains blocks the child, which would turn a chatty method into a
    wall-clock timeout instead of the honest result it produced.
    """
    buf = bytearray()
    while True:
        chunk = await stream.read(READ_CHUNK_BYTES)
        if not chunk:
            return bytes(buf)
        if len(buf) < keep:
            buf.extend(chunk[: keep - len(buf)])


async def _exchange(proc, payload: bytes) -> tuple[bytes, bytes, bool]:
    """Feed stdin and drain both pipes concurrently, capping what is buffered.

    Returns (stdout, stderr, output_overflowed). On overflow the child is killed
    where it stands — it has already forfeited the cap, and waiting for it to
    finish writing is exactly what we are refusing to do.
    """
    feeder = asyncio.ensure_future(_feed_stdin(proc, payload))
    errors = asyncio.ensure_future(_drain_capped(proc.stderr, MAX_STDERR_BYTES))
    try:
        stdout, overflowed = await _read_capped(proc.stdout, MAX_OUTPUT_BYTES)
        if overflowed:
            proc.kill()
        stderr = await errors
        await proc.wait()
        return stdout, stderr, overflowed
    finally:
        for task in (feeder, errors):
            task.cancel()


async def _verify_pack(db: AsyncSession, pack_id: uuid.UUID, pack_dir: Path) -> None:
    """Re-check the installed pack's content hash before trusting its code."""
    pack = await db.get(Pack, pack_id)
    if pack is None:
        return
    label = f"{pack.slug}@{pack.version}"
    if not pack.content_hash:
        log.warning(
            "pack %s has no pinned content hash (installed before integrity pinning); "
            "reinstall it to pin one",
            label,
        )
        return
    try:
        verify_pack_integrity(pack_dir, pack.content_hash, pack_label=label)
    except PackIntegrityError as e:
        raise MethodError(str(e))


def _flatten_finding(f: Finding) -> dict:
    row: dict = {"finding_id": str(f.id), "status": f.status}
    for k, v in (f.subject or {}).items():
        if isinstance(v, (str, int, float, bool)):
            row[k] = v
    for k, v in (f.payload or {}).items():
        if isinstance(v, (str, int, float, bool)):
            row[k] = v
    return row


async def _materialize_inputs(
    db: AsyncSession, project_id: uuid.UUID, input_specs: list[str]
) -> tuple[dict[str, list[dict]], dict]:
    inputs: dict[str, list[dict]] = {}
    summary: dict = {}
    for spec in input_specs:
        if spec.startswith("findings:"):
            schema_slug = spec.split(":", 1)[1]
            findings = (
                (
                    await db.execute(
                        select(Finding)
                        .where(
                            Finding.project_id == project_id,
                            Finding.schema_slug == schema_slug,
                        )
                        .order_by(Finding.created_at)
                    )
                )
                .scalars()
                .all()
            )
            rows = [_flatten_finding(f) for f in findings]
        else:
            ds = (
                await db.execute(
                    select(Dataset).where(
                        Dataset.project_id == project_id, Dataset.name == spec
                    )
                )
            ).scalar_one_or_none()
            if ds is None:
                raise MethodError(f"Required input dataset '{spec}' not found in project")
            db_rows = (
                (
                    await db.execute(
                        select(DatasetRow)
                        .where(DatasetRow.dataset_id == ds.id)
                        .order_by(DatasetRow.row_index)
                    )
                )
                .scalars()
                .all()
            )
            rows = [r.data for r in db_rows]
        inputs[spec] = rows
        summary[spec] = {
            "rows": len(rows),
            "sha256": hashlib.sha256(
                json.dumps(rows, sort_keys=True, default=str).encode()
            ).hexdigest()[:16],
        }
    return inputs, summary


async def execute_method(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    pack_id: uuid.UUID,
    pack_dir: str,
    method_spec: dict,  # entry from the stored pack manifest
    params: dict,
    run_id: uuid.UUID | None = None,
) -> MethodRun:
    """Run one method under the deterministic contract and persist the manifest."""
    entrypoint = Path(pack_dir) / method_spec["entrypoint"]
    if not entrypoint.is_file():
        raise MethodError(f"Method entrypoint missing on disk: {entrypoint}")
    code_sha = hashlib.sha256(entrypoint.read_bytes()).hexdigest()

    inputs, input_summary = await _materialize_inputs(
        db, project_id, method_spec.get("inputs", [])
    )
    payload = json.dumps({"params": params, "inputs": inputs}, default=str).encode()

    record = MethodRun(
        project_id=project_id,
        pack_id=pack_id,
        run_id=run_id,
        method_slug=method_spec["slug"],
        params=params,
        code_sha=code_sha,
        input_summary=input_summary,
    )

    start = time.monotonic()
    try:
        # Integrity first: a drifted pack must never produce a "completed" run.
        await _verify_pack(db, pack_id, Path(pack_dir))
        prefix = await network_isolation_prefix()
        proc = await asyncio.create_subprocess_exec(
            *prefix,  # empty, or `unshare --net --` on a capable Linux host
            sys.executable,
            "-I",  # isolated: no env vars, no user site-packages
            str(entrypoint),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,  # diagnostics only, truncated below
            cwd=str(entrypoint.parent),
            env={},
            close_fds=True,  # nothing but stdin/stdout/stderr crosses into the child
            preexec_fn=_set_limits,
        )
        try:
            stdout, stderr, overflowed = await asyncio.wait_for(
                _exchange(proc, payload), timeout=float(method_spec.get("timeout_seconds", 60))
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise MethodError("Method timed out")
        # Checked before the return code: a killed writer exits non-zero, and the
        # honest diagnosis is the cap it broke, not the signal we sent it.
        if overflowed:
            raise MethodError(
                f"Method output exceeds size cap ({MAX_OUTPUT_BYTES} bytes) and was cut off"
            )
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace")[:MAX_STDERR_CHARS]
            raise MethodError(f"Method exited {proc.returncode}: {detail}")
        try:
            result = json.loads(stdout.decode())
            rows = result["rows"]
            assert isinstance(rows, list)
        except (json.JSONDecodeError, KeyError, AssertionError) as e:
            raise MethodError(f"Method output is not valid {{'rows': [...]}} JSON: {e}")
        if len(rows) > MAX_OUTPUT_ROWS:
            raise MethodError(f"Method returned {len(rows)} rows (cap {MAX_OUTPUT_ROWS})")

        record.output = rows
        record.output_hash = hashlib.sha256(
            json.dumps(rows, sort_keys=True, default=str).encode()
        ).hexdigest()
        record.row_count = len(rows)
        record.status = "completed"
    except MethodError as e:
        record.status = "failed"
        record.error = str(e)
    finally:
        record.duration_ms = int((time.monotonic() - start) * 1000)

    db.add(record)
    await db.flush()
    if record.status == "failed":
        raise MethodError(record.error or "method failed")
    return record
