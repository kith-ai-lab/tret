"""Materialize a `DraftPack` DB row into pack files, in whichever shape the
caller needs: a directory on disk (for `validate_pack`/`install_pack`, which
only ever read a directory — see loader.py's own module docstring) or tar.gz
bytes (for the builder's `GET .../export`, which must produce exactly the
archive layout `packs/archive.py::extract_pack_archive` accepts: `pack.yaml`
and every other file at the archive's own root, no wrapping top-level
directory).

A draft's `manifest_json`/`files` never reach `validate_pack`/`install_pack`
directly — those two functions are unchanged and only ever read a real
directory, by design. Nothing here is a new validation path: it is the same
directory shape `tret packs validate <dir>` and an archive upload already
produce, just built from JSONB instead of an author's own filesystem or an
uploaded tarball, so a draft with a given flaw produces the exact same error
text a filesystem pack with that flaw would.
"""
from __future__ import annotations

import base64
import binascii
import io
import tarfile
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from tret.db.models import DraftPack


# Root-level names `files{}` may never claim: writing to these would let a
# PATCH body overwrite something `materialize_draft`/`build_draft_archive`
# generate themselves rather than take from `files`. `pack.yaml` is the one
# that matters today — it is the generated manifest (`_manifest_yaml_bytes`),
# and letting `files["pack.yaml"]` win would smuggle a manifest arbitrary
# methods/etc past `_reject_methods` (which only ever inspects
# `PatchDraftBody.manifest_json`, never `files`) and, at test-install, let a
# draft's `files["pack.yaml"]["version"]` silently override the
# `{version}+draft.{n}` `version_override` `test_install_draft` computes,
# risking a collision with — and clobber of — a real installed pack at that
# exact (workspace, slug, version). Checked case-insensitively: a
# case-insensitive filesystem (macOS, Windows) would let `Pack.YAML` collide
# with the generated `pack.yaml` at the OS level even though the two strings
# differ.
RESERVED_ROOT_NAMES = frozenset({"pack.yaml"})


class DraftPathError(Exception):
    """A relpath in a draft's `files` dict is unsafe to write out.

    Raised at PATCH time (`api/pack_builder.py`), so a bad path is refused
    before it is ever saved — and defensively again here, so a path that
    somehow reached the database some other way still cannot escape the
    materialization directory or the export archive.
    """


class DraftFileContentError(Exception):
    """A draft file's stored content value (`PatchDraftBody.files[relpath]`)
    is malformed: `{"b64": ...}` whose `"b64"` isn't a string, or is a string
    that isn't valid base64. Raised at PATCH time (`api/pack_builder.py`),
    same posture as `DraftPathError` — refused before it is ever saved,
    rather than degrading to empty content (which `file_bytes` does for a
    content value that is some *other* unexpected shape entirely, e.g. a
    bare `null` slipping through outside a `{"b64": ...}` wrapper) or raising
    an un-typed exception `materialize_draft`/`build_draft_archive` would
    otherwise let escape as a 500.
    """


def validate_draft_relpath(relpath: str) -> None:
    """Reject anything that could write outside a materialization directory
    or smuggle a path-escape into the exported archive: absolute paths, `..`
    segments, backslashes (not a valid separator here even on a POSIX
    filesystem — accepting one would let a Windows-style escape slip past a
    POSIX-only check), NUL bytes (POSIX filenames may not contain one;
    `Path.write_bytes`/`tarfile` would either raise an opaque `ValueError`
    or, worse on some platforms, silently truncate the name at the NUL), and
    empty segments (a leading, trailing, or doubled `/`). The same posture as
    `packs/archive.py`'s zip-slip checks, applied one layer earlier: a
    draft's `files` dict is exactly as untrusted as an archive member's
    name, just arriving over PATCH instead of tar.

    Also rejects `RESERVED_ROOT_NAMES` (`pack.yaml`) — see that constant's
    own docstring for why a `files` entry may never claim a name
    `materialize_draft`/`build_draft_archive` generate themselves.

    Does NOT check for shadowing against a draft's *other* files (one path a
    prefix-directory of another) — that needs the whole merged `files` dict,
    not one relpath in isolation. See `validate_no_path_shadowing`, called
    separately once PATCH has merged the incoming keys into the draft's
    existing ones.
    """
    if not relpath:
        raise DraftPathError("a file path may not be empty")
    if "\x00" in relpath:
        raise DraftPathError(f"file path {relpath!r} may not contain a NUL byte")
    if "\\" in relpath:
        raise DraftPathError(f"file path {relpath!r} may not contain a backslash")
    if relpath.startswith("/"):
        raise DraftPathError(f"file path {relpath!r} may not be absolute")
    parts = relpath.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise DraftPathError(f"file path {relpath!r} has an invalid path segment")
    if relpath.strip().lower() in RESERVED_ROOT_NAMES:
        raise DraftPathError(
            f"file path {relpath!r} is reserved — pack.yaml is generated from the "
            "draft's manifest_json, not one of its files"
        )


def validate_no_path_shadowing(relpaths) -> None:
    """Reject a `files` dict where one path is both a file and, via another
    path, implied to be a directory — `"a.md"` alongside `"a.md/b.md"`, e.g.
    Each is individually a fine `validate_draft_relpath` path; together they
    are impossible to materialize (`target.parent.mkdir(parents=True)` would
    try to `mkdir` a file that already exists at `"a.md"`) and impossible to
    tar sensibly (an extractor sees a regular-file entry and a same-named
    directory-implying entry for the identical path).

    Takes the *whole* merged `files` dict's keys (existing plus incoming,
    post-merge) rather than validating an incoming key in isolation, since
    the conflict is between two paths, not a property of either one alone —
    called once from `api/pack_builder.py`'s PATCH handler, after the merge,
    before the draft is saved.
    """
    paths = set(relpaths)
    for path in paths:
        prefix = ""
        for part in path.split("/")[:-1]:
            prefix = f"{prefix}/{part}" if prefix else part
            if prefix in paths:
                raise DraftPathError(
                    f"file paths {prefix!r} and {path!r} shadow each other: {prefix!r} is "
                    f"a file, but {path!r} needs {prefix!r} to be a directory"
                )


def file_bytes(content: object) -> bytes:
    """A draft file's stored JSON value, decoded: `{"b64": "..."}` for binary
    content, a plain `str` for text — see the DraftPack model docstring for
    the shape PATCH accepts. A `{"b64": ...}` whose value isn't valid base64
    (not a string, or a string that doesn't decode) raises
    `DraftFileContentError` rather than degrading — see that class's own
    docstring for why. Any *other* unexpected shape (a bare `null`, e.g.)
    still degrades to empty content, since by the time this runs a value in
    that other shape already passed PATCH's own validation and there is
    nothing typed to say is wrong with it. Public: also used by
    `api/pack_builder.py`'s PATCH handler to enforce the per-file/total size
    caps against the same decoded byte count this module writes out.
    """
    if isinstance(content, dict) and "b64" in content:
        raw = content["b64"]
        if not isinstance(raw, str):
            raise DraftFileContentError(
                f"file content's 'b64' value must be a string, got {type(raw).__name__}"
            )
        try:
            return base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise DraftFileContentError(f"file content is not valid base64: {exc}") from exc
    if isinstance(content, str):
        return content.encode("utf-8")
    return b""


def _manifest_yaml_bytes(draft: DraftPack, *, version_override: str | None) -> bytes:
    manifest = dict(draft.manifest_json or {})
    if version_override is not None:
        manifest["version"] = version_override
    return yaml.safe_dump(manifest, sort_keys=False).encode("utf-8")


def materialize_draft(
    draft: DraftPack, *, version_override: str | None = None, root: Path | None = None
) -> Path:
    """Write `draft` out to a directory and return its path.

    `root` is created (with parents) if given; otherwise a fresh
    `tempfile.mkdtemp()` directory is used. Passing `root` matters for
    test-install: it materializes directly under `PackStorage`'s own root
    (`tret.packs.storage.PackStorage.staging_dir()`) so the rename that later
    swaps it into the pack's permanent home is same-filesystem — a plain
    `tempfile.mkdtemp()` directory (typically under `/tmp`) is not guaranteed
    to share a filesystem with `TRET_STORAGE_DIR`, and a cross-device rename
    raises `OSError(EXDEV)` instead of completing atomically.

    The caller owns cleanup (`shutil.rmtree`). On validate/export that means
    unconditionally, once the action is done; on test-install, only once the
    directory (or its successor, after `install_pack` and the swap into
    permanent storage) is no longer needed — the same "safe unconditionally"
    shape `install_pack_from_archive`'s own `finally` documents.
    """
    if root is None:
        root = Path(tempfile.mkdtemp(prefix="tret-draft-"))
    else:
        root.mkdir(parents=True, exist_ok=True)

    # Files first, the generated manifest last (belt and braces): `files{}`
    # can never legitimately contain a `RESERVED_ROOT_NAMES` key —
    # `validate_draft_relpath` rejects it at PATCH time, and the explicit
    # skip below drops it here too, for a draft whose `files` reached the
    # database some other way (a pre-fix row, a direct write) rather than
    # raising and failing every action on it outright. Writing the manifest
    # *after* the loop is the third layer: even if a reserved key somehow
    # slipped past that skip too, it would already be on disk under
    # "pack.yaml" by the time this line runs, and this write replaces it —
    # the generated manifest always has the last word. The reverse order
    # (manifest first, files after) is what would let `files["pack.yaml"]`
    # clobber it.
    for relpath, content in (draft.files or {}).items():
        if relpath.strip().lower() in RESERVED_ROOT_NAMES:
            continue
        validate_draft_relpath(relpath)
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(file_bytes(content))
    (root / "pack.yaml").write_bytes(
        _manifest_yaml_bytes(draft, version_override=version_override)
    )
    return root


def build_draft_archive(draft: DraftPack) -> bytes:
    """tar.gz bytes for `GET /{id}/export`: `pack.yaml` and every draft file
    at the archive's own root (no wrapping directory) — the layout
    `packs/archive.py::extract_pack_archive` accepts directly (it also
    tolerates a single wrapping directory via `loader.py::_unwrap_sole_directory`,
    but writing flat means this archive is byte-for-byte the shape a
    from-scratch `tar czf` of the draft's own files would produce), so the
    resulting file is also exactly what `POST /api/packs/install/archive`
    (and, later, a marketplace submission) expect as input.
    """
    buf = io.BytesIO()
    mtime = int(time.time())

    def _add(tar: tarfile.TarFile, arcname: str, data: bytes) -> None:
        info = tarfile.TarInfo(name=arcname)
        info.size = len(data)
        info.mtime = mtime
        tar.addfile(info, io.BytesIO(data))

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add(tar, "pack.yaml", _manifest_yaml_bytes(draft, version_override=None))
        for relpath, content in (draft.files or {}).items():
            # Belt and braces, same reasoning as materialize_draft's write
            # order above: validate_draft_relpath already rejects this key at
            # PATCH time, so this skip is defense for a draft whose `files`
            # reached the database some other way — a duplicate "pack.yaml"
            # tar member must never be produced, since which of two same-
            # named entries an extractor honours is not this module's call
            # to make. Checked, and skipped, before validate_draft_relpath
            # so a legacy/bypassed row still exports cleanly instead of
            # failing the whole archive on a key that must simply be dropped.
            if relpath.strip().lower() in RESERVED_ROOT_NAMES:
                continue
            validate_draft_relpath(relpath)
            _add(tar, relpath, file_bytes(content))
    return buf.getvalue()
