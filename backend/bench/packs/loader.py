"""Pack loading: parse + validate pack.yaml, hash doctrine, install to DB,
seed sample datasets. Schemas are inlined into the stored manifest so the
engine never re-reads pack files for validation at runtime.
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
from bench.engine.context import doctrine_sha
from bench.engine.tools import get_builtin_tools
from bench.packs.schema import PackManifest


class PackValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


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
        if task.terminal_tool and task.terminal_tool not in builtins:
            errors.append(f"task '{task.slug}': terminal_tool '{task.terminal_tool}' is not a known tool")
        for tool in task.tools:
            if tool not in builtins:
                errors.append(f"task '{task.slug}': unknown tool '{tool}'")

    for ds in manifest.datasets:
        if not (pack_dir / ds.file).exists():
            errors.append(f"dataset file missing: {ds.file}")

    return manifest, schemas, errors


async def install_pack(
    db: AsyncSession, pack_dir: Path, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> Pack:
    """Idempotent by (workspace, slug, version). Seeds datasets into project_id."""
    pack_dir = pack_dir.resolve()
    manifest, schemas, errors = validate_pack(pack_dir)
    if errors:
        raise PackValidationError(errors)

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
        return existing

    stored_manifest = manifest.model_dump()
    stored_manifest["schemas"] = schemas
    # Resolve each task's output_schema path to its slug for the engine/context.
    for task in stored_manifest["task_types"]:
        if task.get("output_schema"):
            task["output_schema_slug"] = Path(task["output_schema"]).stem.removesuffix(".schema")

    pack = Pack(
        workspace_id=workspace_id,
        slug=manifest.pack,
        version=manifest.version,
        doctrine_sha=doctrine_sha(pack_dir, manifest.doctrine),
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
