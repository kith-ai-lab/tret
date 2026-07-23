"""Deterministic method execution — the compute side of the deterministic lane.

Methods are pack-authored Python scripts (operator-vetted, versioned with the
pack). The agent may invoke them with parameters; it never writes code. Each
execution is manifest-pinned: params, code sha, input summary, output hash.

Contract with the script:
  stdin:  {"params": {...}, "inputs": {"<name>": [row, ...]}}
  stdout: {"rows": [{...}, ...]}   (flat dicts; stdlib only; pure function)

Sandbox (v1): isolated interpreter (`python -I`), empty environment, CPU and
memory rlimits, wall-clock timeout, output caps, no DB access — inputs are
materialized by the runner and passed in. Pack code is operator-trusted (same
stance as pack installation itself); OS-level isolation is the v2 hardening.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import resource
import sys
import time
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bench.db.models import Dataset, DatasetRow, Finding, MethodRun

MAX_OUTPUT_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_ROWS = 2000


class MethodError(Exception):
    pass


def _set_limits() -> None:  # runs in the child before exec
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
    except (ValueError, OSError):
        pass  # RLIMIT_AS unsupported on some platforms (macOS)


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
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",  # isolated: no env vars, no user site-packages
            str(entrypoint),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(entrypoint.parent),
            env={},
            preexec_fn=_set_limits,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(payload), timeout=float(method_spec.get("timeout_seconds", 60))
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise MethodError("Method timed out")
        if proc.returncode != 0:
            raise MethodError(
                f"Method exited {proc.returncode}: {stderr.decode(errors='replace')[:500]}"
            )
        if len(stdout) > MAX_OUTPUT_BYTES:
            raise MethodError("Method output exceeds size cap")
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
