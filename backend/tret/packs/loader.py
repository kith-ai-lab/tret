"""Pack loading: parse + validate pack.yaml, hash doctrine, install to DB,
seed sample datasets. Schemas are inlined into the stored manifest so the
engine never re-reads pack files for validation at runtime.

Validation also runs the static method scan (`tret.packs.safety`) — a
deterrent against non-deterministic method code, not a sandbox — and install
pins a content hash over every pack file (`tret.packs.integrity`).
"""
from __future__ import annotations

import csv
import errno
import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import jsonschema
import yaml

# Boundary note: `doctrine:` selector syntax (`file.md#Heading`) is part of the
# *pack format*, so parsing and validating it belongs beside the manifest schema
# in this package rather than in the engine that consumes the result. It lives in
# engine/context.py for historical reasons, and the same unit of doctrine is
# called a `section` there and a "heading" in the validation messages here. Moving
# the parser into tret/packs/ (engine importing from packs, not the reverse)
# would fix both; it is an engine-side edit, so it is recorded here rather than
# done piecemeal.
from tret.engine.context import doctrine_sha, parse_doctrine_selector, select_doctrine_text
from tret.packs.archive import PackArchiveError, extract_pack_archive
from tret.packs.integrity import pack_content_hash
from tret.packs.safety import scan_method_file
from tret.packs.schema import PackManifest
from tret.packs.storage import PackStorage

if TYPE_CHECKING:
    # SQLAlchemy, the ORM models, and `tret.engine.tools` (which pulls in the
    # whole tool-execution engine, itself SQLAlchemy-backed) are deferred into
    # the functions that need them so that *importing* this module never
    # requires the server extra. Note the limit of that guarantee: *calling*
    # `validate_pack` or `install_pack` still needs `tret[server]`, because
    # both reach `get_builtin_tools()` and the engine behind it. Core-only
    # installs get `tret packs hash`; `tret packs validate` needs the extra.
    from sqlalchemy.ext.asyncio import AsyncSession

    from tret.db.models import Pack

log = logging.getLogger("tret.packs.loader")


class PackValidationError(Exception):
    def __init__(self, errors: list[str], *, pack_root: Path | None = None):
        self.errors = errors
        # The resolved directory `validate_pack` ran against — carried along
        # so a caller whose directory is a server-side staging path (an
        # archive install; never a path-install, where the directory *is* the
        # operator's own and worth naming) can strip its absolute prefix
        # before the errors reach a 4xx body. See
        # `api.packs._relative_pack_errors`, the one place that actually does
        # the stripping.
        self.pack_root = pack_root
        super().__init__("; ".join(errors))


class PackInstallConflict(Exception):
    """Two installs of the same pack directory collided and disagreed: a
    concurrent re-install finished first with *different* content than this
    one was about to write. Distinct from `PackArchiveError` (a malformed or
    hostile upload, mapped to 422) — this is a race with another legitimate
    install, mapped to 409 so the caller knows a retry is the right move, not
    a fixed archive."""


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
    from tret.engine.tools import get_builtin_tools

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
    from sqlalchemy import select

    from tret.db.models import Dataset, DatasetRow, Pack

    pack_dir = pack_dir.resolve()
    manifest, schemas, errors = validate_pack(pack_dir)
    if errors:
        raise PackValidationError(errors, pack_root=pack_dir)

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


_CONCURRENT_LOSER_ERRNOS = {errno.ENOTEMPTY, errno.EEXIST}


def _unwrap_sole_directory(staging_dir: Path) -> Path:
    """If `staging_dir` holds exactly one entry and it is a directory (no
    sibling files), treat that directory as the pack root instead — the
    natural shape `tar czf pack.tar.gz mypack/` produces, where `pack.yaml`
    lives one level down from the archive's own top. A staging directory that
    already has `pack.yaml` (or anything else) at its top is returned
    unchanged."""
    entries = list(staging_dir.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return staging_dir


def _swap_into_final_dir(source: Path, final_dir: Path, *, content_hash: str) -> None:
    """Make `final_dir` become `source`, trash-swapping whatever was already
    there instead of `rmtree`-then-`rename`: that older sequence has a window
    where `final_dir` exists at neither the old nor the new content, which a
    concurrent reader (a run reading doctrine mid-request) or a losing
    concurrent installer (see below) could observe.

    Sequence: rename the existing `final_dir` (if any) out of the way to a
    freshly-named trash directory, rename `source` into `final_dir`, then
    best-effort delete the trash — a leaked trash directory is logged, not
    raised, since the swap itself already succeeded by that point.

    Two ways this can fail:

    - The rename of `source` onto `final_dir` fails with ENOTEMPTY/EEXIST:
      another install of this same pack id finished first and recreated
      `final_dir` in the gap between us trashing the old one and renaming our
      own content in. Not necessarily an error — clean up our own `source`
      and compare `content_hash` (computed from `source` by the caller,
      before this function ever touches it) against the now-present
      directory's actual hash. Equal hashes mean both installs agreed on the
      same content and we simply lost the race to write it: treat that as
      success. A mismatch is a genuine conflict, raised as
      `PackInstallConflict` for the API layer to map to 409.
    - Any other failure: restore the trashed original (if we trashed one)
      before re-raising, so a failed swap never leaves `final_dir` missing
      entirely.
    """
    trash_dir: Path | None = None
    if final_dir.exists():
        trash_dir = final_dir.with_name(f".trash-{uuid.uuid4().hex}")
        final_dir.rename(trash_dir)

    try:
        source.rename(final_dir)
    except OSError as e:
        if e.errno in _CONCURRENT_LOSER_ERRNOS:
            shutil.rmtree(source, ignore_errors=True)
            if trash_dir is not None:
                shutil.rmtree(trash_dir, ignore_errors=True)
            winner_hash = pack_content_hash(final_dir)
            if winner_hash == content_hash:
                return
            raise PackInstallConflict(
                "another install of this pack finished first with different content — retry"
            ) from e
        if trash_dir is not None:
            try:
                trash_dir.rename(final_dir)
            except OSError:
                log.error(
                    "failed to restore %s from trash %s after a failed pack directory swap",
                    final_dir, trash_dir,
                )
        raise
    else:
        if trash_dir is not None:
            try:
                shutil.rmtree(trash_dir)
            except OSError:
                log.warning("failed to remove trashed pack directory %s", trash_dir)


async def install_pack_from_archive(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    archive_bytes: bytes,
    *,
    expected_content_hash: str | None = None,
) -> Pack:
    """Install a pack shipped as a tar.gz archive (an upload, or later a
    marketplace download) rather than a path on the server's own filesystem.

    Sequence: extract to a staging directory under
    `storage_dir/packs/.staging-{uuid}/` (`packs/archive.py`, which rejects
    anything hostile in the archive itself), unwrap a sole top-level directory
    if the archive was authored `tar czf pack.tar.gz mypack/`
    (`_unwrap_sole_directory`) -> run the UNCHANGED `validate_pack`/`install_pack`
    above against that pack root, exactly as the path-install endpoint does
    against an operator's directory -> when the caller supplied
    `expected_content_hash` (a marketplace catalog's declared hash), verify the
    pack root's actual content hash matches it before trusting anything
    further — defense in depth against a corrupted transfer or a catalog that
    lied -> atomically swap the pack root into its permanent home
    `storage_dir/packs/{pack.id}/` (`_swap_into_final_dir`, a trash-and-rename
    that never leaves the permanent directory transiently missing) and update
    `Pack.source_path` to point there, then commit.

    A re-install of the same (workspace, slug, version) reuses the existing
    `Pack` row (that is `install_pack`'s own idempotency, unchanged), so
    `pack.id` — and therefore the permanent directory — is the same as
    before; the old directory's contents are replaced by the new archive's,
    not merged with them.

    Any failure along the way — a hostile archive, a validation error, a hash
    mismatch, or the swap itself — removes the staging directory before the
    exception propagates (a `PackInstallConflict`'s "we lost the race but
    agreed on content" outcome is not a failure, and still cleans up the same
    way). Nothing partial is left for a later install to trip over.
    """
    storage = PackStorage()
    staging_dir = storage.staging_dir()

    try:
        extract_pack_archive(archive_bytes, staging_dir)
        pack_root = _unwrap_sole_directory(staging_dir)

        if expected_content_hash is not None:
            actual_hash = pack_content_hash(pack_root)
            if actual_hash != expected_content_hash:
                raise PackArchiveError(
                    f"archive content hash {actual_hash[:16]}… does not match the "
                    f"expected {expected_content_hash[:16]}… — refusing to install"
                )

        # install_pack commits internally with source_path still pointing at
        # pack_root (inside staging_dir). Known residual risk, accepted rather
        # than engineered around: if the swap below fails in the "any other
        # failure" branch after a successful restore, the committed Pack row
        # is briefly left pointing at a staging directory this function's
        # `finally` then deletes out from under it. install_pack is required
        # to stay UNCHANGED (it is the exact function the path-install
        # endpoint also calls), so there is no savepoint boundary available
        # here to undo its commit.
        pack = await install_pack(db, pack_root, workspace_id, project_id)

        final_dir = storage.path_for(pack.id)
        _swap_into_final_dir(pack_root, final_dir, content_hash=pack.content_hash)
        pack.source_path = str(final_dir)
        await db.commit()
        return pack
    finally:
        # Safe unconditionally: on success `pack_root` (== `staging_dir` when
        # nothing was unwrapped) has already been renamed away, so there is
        # nothing left here but `ignore_errors` no-ops on the missing path or
        # an unwrap's now-empty wrapper; on any failure this is exactly the
        # cleanup that always ran here.
        shutil.rmtree(staging_dir, ignore_errors=True)
