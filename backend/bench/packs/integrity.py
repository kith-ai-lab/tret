"""Pack integrity pinning: a content hash over every file in the pack directory.

`doctrine_sha` pins the reasoning rules; this pins *everything* — methods,
schemas, datasets, templates, pack.yaml. It is computed at install time, stored
on the Pack row, and re-verified before a method executes, so editing pack code
under a running deployment fails loudly instead of silently changing results.

This detects tampering and accidental drift by an operator who can write to the
pack directory. It is not a signature: anyone who can edit pack files can also
reinstall the pack to refresh the hash. Signed packs are a later step.

Refresh path after an intentional pack edit: reinstall the pack (restart, which
runs the idempotent bootstrap, or `POST /api/packs/install`). `bench packs hash`
prints the hash a reinstall would store.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

# Build artefacts and editor/VCS noise are excluded so a hash survives a `find`
# on a developer's machine. Anything a method could read is included.
IGNORED_DIRS = {"__pycache__", ".git", ".hg", ".svn", ".venv", "node_modules", ".mypy_cache",
                ".pytest_cache", ".ruff_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo", ".swp"}
IGNORED_NAMES = {".DS_Store"}


class PackIntegrityError(Exception):
    """Raised when a pack's on-disk content no longer matches its pinned hash."""


def iter_pack_files(pack_dir: Path) -> list[Path]:
    """Every content file in the pack, sorted by relative POSIX path."""
    files: list[Path] = []
    for path in pack_dir.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(pack_dir)
        if any(part in IGNORED_DIRS for part in rel.parts[:-1]):
            continue
        if path.suffix in IGNORED_SUFFIXES or path.name in IGNORED_NAMES:
            continue
        files.append(path)
    return sorted(files, key=lambda p: p.relative_to(pack_dir).as_posix())


def pack_content_hash(pack_dir: Path) -> str:
    """sha256 over (relative path, size, bytes) of every pack file, path-sorted."""
    h = hashlib.sha256()
    for path in iter_pack_files(pack_dir):
        rel = path.relative_to(pack_dir).as_posix().encode()
        data = path.read_bytes()
        h.update(len(rel).to_bytes(4, "big"))
        h.update(rel)
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


# (dir, stat signature) -> hash. Avoids re-reading every pack file on each
# method invocation while still noticing edits (mtime/size change).
_cache: dict[str, tuple[tuple, str]] = {}


def _stat_signature(pack_dir: Path) -> tuple:
    return tuple(
        (p.relative_to(pack_dir).as_posix(), p.stat().st_mtime_ns, p.stat().st_size)
        for p in iter_pack_files(pack_dir)
    )


def cached_content_hash(pack_dir: Path) -> str:
    """`pack_content_hash` memoised on the directory's stat signature."""
    key = str(pack_dir)
    signature = _stat_signature(pack_dir)
    hit = _cache.get(key)
    if hit is not None and hit[0] == signature:
        return hit[1]
    digest = pack_content_hash(pack_dir)
    _cache[key] = (signature, digest)
    return digest


def clear_cache() -> None:
    _cache.clear()


def verify_pack_integrity(pack_dir: Path, expected: str | None, *, pack_label: str) -> None:
    """Raise PackIntegrityError if the pack on disk drifted from its pinned hash.

    `expected` of None means the pack predates integrity pinning (installed by
    an older bench); callers should warn rather than fail so upgrades are not
    hard blocks — reinstalling the pack pins it.
    """
    if not expected:
        return
    actual = cached_content_hash(pack_dir)
    if actual != expected:
        raise PackIntegrityError(
            f"Pack '{pack_label}' fails its integrity check: files under {pack_dir} have "
            f"changed since installation (pinned {expected[:16]}…, on disk {actual[:16]}…). "
            "Deterministic results cannot be trusted. Reinstall the pack to accept the new "
            "content (restart bench, or POST /api/packs/install), or restore the original files."
        )
