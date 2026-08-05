"""Pack loading: parse + validate pack.yaml, hash doctrine, install to DB,
seed sample datasets. Schemas are inlined into the stored manifest so the
engine never re-reads pack files for validation at runtime.

Validation also runs the static method scan (`bench.packs.safety`) — a
deterrent against non-deterministic method code, not a sandbox — and install
pins a content hash over every pack file (`bench.packs.integrity`).
"""
from __future__ import annotations

import csv
import json
import uuid
from pathlib import Path

import jsonschema
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bench.db.models import Dataset, DatasetRow, Pack
from bench.engine.context import doctrine_sha, parse_doctrine_selector, select_doctrine_text
from bench.engine.tools import get_builtin_tools
from bench.packs.integrity import pack_content_hash
from bench.packs.safety import scan_method_file
from bench.packs.schema import PackManifest


class PackValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _doctrine_selector_errors(
    pack_dir: Path, manifest: PackManifest, task_slug: str, selector: str
) -> list[str]:
    """A task's `doctrine:` entry must name a pack doctrine file (and a real section)."""
    rel, section = parse_doctrine_selector(selector)
    if rel not in manifest.doctrine:
        return [
            f"task '{task_slug}': doctrine selector '{selector}' references '{rel}', which is "
            "not in the pack's doctrine list"
        ]
    path = pack_dir / rel
    if section is None or not path.exists():
        return []
    _, matched, unresolved = select_doctrine_text(path.read_text(), [section])
    if unresolved:
        return [
            f"task '{task_slug}': doctrine selector '{selector}' names a heading that does not "
            f"exist in {rel}"
        ]
    return []


def validate_pack(pack_dir: Path) -> tuple[PackManifest, dict[str, dict], list[str]]:
    """Returns (manifest, schemas-by-slug, errors). Raises nothing."""
    errors: list[str] = []
    manifest_path = pack_dir / "pack.yaml"
    if not manifest_path.exists():
        return None, {}, [f"{manifest_path} does not exist"]  # type: ignore[return-value]
    try:
        manifest = PackManifest.model_validate(yaml.safe_load(manifest_path.read_text()))
    except Exception as e:
        return None, {}, [f"pack.yaml invalid: {e}"]  # type: ignore[return-value]

    for rel in manifest.doctrine:
        if not (pack_dir / rel).exists():
            errors.append(f"doctrine file missing: {rel}")

    schemas: dict[str, dict] = {}
    builtins = set(get_builtin_tools())
    for task in manifest.task_types:
        if task.output_schema:
            path = pack_dir / task.output_schema
            slug = Path(task.output_schema).stem.removesuffix(".schema")
            if not path.exists():
                errors.append(f"task '{task.slug}': schema file missing: {task.output_schema}")
            else:
                try:
                    schema = json.loads(path.read_text())
                    jsonschema.Draft202012Validator.check_schema(schema)
                    schemas[slug] = schema
                except Exception as e:
                    errors.append(f"task '{task.slug}': invalid JSON Schema: {e}")
        for selector in task.doctrine:
            errors.extend(_doctrine_selector_errors(pack_dir, manifest, task.slug, selector))
        if task.terminal_tool and task.terminal_tool not in builtins:
            errors.append(f"task '{task.slug}': terminal_tool '{task.terminal_tool}' is not a known tool")
        # A terminal tool the task never offers the model can never be called, so
        # the run would nudge once and then end `completed_without_output` for
        # ever — silently, and only on that task type. Fail at install instead.
        # Only checkable when the task declares its own tools: an empty `tools`
        # falls back to the harness's tool_names, which no pack can see.
        if task.terminal_tool and task.tools and task.terminal_tool not in task.tools:
            errors.append(
                f"task '{task.slug}': terminal_tool '{task.terminal_tool}' is not in the task's "
                f"tools {sorted(task.tools)} — the model could never call it"
            )
        for tool in task.tools:
            if tool not in builtins:
                errors.append(f"task '{task.slug}': unknown tool '{tool}'")

    for ds in manifest.datasets:
        if not (pack_dir / ds.file).exists():
            errors.append(f"dataset file missing: {ds.file}")

    dataset_names = {d.name for d in manifest.datasets}
    seen_methods: set[str] = set()
    for m in manifest.methods:
        if m.slug in seen_methods:
            errors.append(f"duplicate method slug '{m.slug}'")
        seen_methods.add(m.slug)
        entrypoint = pack_dir / m.entrypoint
        if not entrypoint.exists():
            errors.append(f"method '{m.slug}': entrypoint missing: {m.entrypoint}")
        else:
            for violation in scan_method_file(entrypoint, label=m.entrypoint):
                errors.append(f"method '{m.slug}': {violation}")
        for spec in m.inputs:
            if not spec.startswith("findings:") and spec not in dataset_names:
                errors.append(
                    f"method '{m.slug}': input '{spec}' is neither a pack dataset "
                    "nor a findings:<schema_slug> reference"
                )

    return manifest, schemas, errors


async def install_pack(
    db: AsyncSession, pack_dir: Path, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> Pack:
    """Idempotent by (workspace, slug, version). Seeds datasets into project_id."""
    pack_dir = pack_dir.resolve()
    manifest, schemas, errors = validate_pack(pack_dir)
    if errors:
        raise PackValidationError(errors)

    stored_manifest = manifest.model_dump()
    stored_manifest["schemas"] = schemas
    # Resolve each task's output_schema path to its slug for the engine/context.
    for task in stored_manifest["task_types"]:
        if task.get("output_schema"):
            task["output_schema_slug"] = Path(task["output_schema"]).stem.removesuffix(".schema")
    sha = doctrine_sha(pack_dir, manifest.doctrine)
    content_hash = pack_content_hash(pack_dir)

    existing = (
        await db.execute(
            select(Pack).where(
                Pack.workspace_id == workspace_id,
                Pack.slug == manifest.pack,
                Pack.version == manifest.version,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Same version, changed content (dev iteration): refresh in place so
        # existing harness references stay valid. Released packs bump versions.
        # This is also the documented way to re-pin the integrity hash after an
        # intentional pack edit.
        if (
            existing.manifest != stored_manifest
            or existing.doctrine_sha != sha
            or existing.content_hash != content_hash
        ):
            existing.manifest = stored_manifest
            existing.doctrine_sha = sha
            existing.content_hash = content_hash
            existing.source_path = str(pack_dir)
        pack = existing
    else:
        pack = Pack(
            workspace_id=workspace_id,
            slug=manifest.pack,
            version=manifest.version,
            doctrine_sha=sha,
            content_hash=content_hash,
            manifest=stored_manifest,
            source_path=str(pack_dir),
        )
        db.add(pack)
    await db.flush()

    for ds_spec in manifest.datasets:
        already = (
            await db.execute(
                select(Dataset).where(
                    Dataset.project_id == project_id, Dataset.name == ds_spec.name
                )
            )
        ).scalar_one_or_none()
        if already is not None:
            continue
        rows = list(csv.DictReader((pack_dir / ds_spec.file).open()))
        dataset = Dataset(
            project_id=project_id,
            pack_id=pack.id,
            name=ds_spec.name,
            schema_json={"columns": list(rows[0].keys()) if rows else []},
            row_count=len(rows),
        )
        db.add(dataset)
        await db.flush()
        for i, row in enumerate(rows):
            db.add(DatasetRow(dataset_id=dataset.id, row_index=i, data=dict(row)))

    await db.commit()
    return pack
