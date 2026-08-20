"""Pack integrity pinning: a content hash over every file in the pack directory.

`doctrine_sha` pins the reasoning rules; this pins *everything* — methods,
schemas, datasets, templates, pack.yaml. It is computed at install time, stored
on the Pack row, and re-verified before a method executes, so editing pack code
under a running deployment fails loudly instead of silently changing results.

This detects tampering and accidental drift by an operator who can write to the
pack directory. It is not a signature: anyone who can edit pack files can also
reinstall the pack to refresh the hash. Signed packs are a later step.

**Symlinks are pinned, not skipped.** They used to be excluded from the walk
entirely, which made them a hole straight through the tripwire: a pinned pack
could ship `analysis.py -> ../elsewhere/analysis.py`, and re-pointing that link
swapped the code a method executes without changing a single byte the hash was
computed over. Every symlink now contributes its path *and* its target string to
the digest, and links are never traversed, so re-pointing one — or adding or
removing one — changes the hash. What a directory hash still cannot cover is the
*content* a link resolves to outside the pack directory: tret refuses to follow
it (a pack could otherwise aim the hasher at `/dev/urandom` or at a file it has
no business reading), so a pack whose data lives behind an external symlink is
pinned by reference only. Keep pack content inside the pack.

Regular-file entries are hashed exactly as they were before symlinks were
covered, so packs without symlinks keep their existing pin and no reinstall is
needed for this change.

Refresh path after an intentional pack edit: reinstall the pack (restart, which
runs the idempotent bootstrap, or `POST /api/packs/install`). `tret packs hash`
prints the hash a reinstall would store.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

# Build artefacts and editor/VCS noise are excluded so a hash survives a `find`
# on a developer's machine. Anything a method could read is included.
IGNORED_DIRS = {"__pycache__", ".git", ".hg", ".svn", ".venv", "node_modules", ".mypy_cache",
                ".pytest_cache", ".ruff_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo", ".swp"}
IGNORED_NAMES = {".DS_Store"}

# Domain separator for symlink entries. Regular files never emit it, so adding
# symlink coverage left every existing file-only pack hash unchanged, and a
# symlink cannot be confused with a file whose bytes happen to equal its target.
_SYMLINK_TAG = b"\x00symlink\x00"


class PackIntegrityError(Exception):
    """Raised when a pack's on-disk content no longer matches its pinned hash."""


@dataclass(frozen=True)
class PackEntry:
    """One pinned thing in a pack: a regular file, or a symlink to anywhere."""

    rel: str  # relative POSIX path within the pack
    path: Path
    is_symlink: bool

    def target(self) -> str:
        """The link's target as written (never resolved, never followed)."""
        return os.readlink(self.path)


def _ignored(name: str, suffix: str) -> bool:
    return suffix in IGNORED_SUFFIXES or name in IGNORED_NAMES


def iter_pack_entries(pack_dir: Path) -> list[PackEntry]:
    """Everything the pin covers, sorted by relative POSIX path.

    Symlinks are recorded and never traversed — including symlinked directories,
    whose contents therefore do not enter the digest (their identity does). That
    also makes the walk immune to a link cycle.
    """
    entries: list[PackEntry] = []

    def walk(directory: Path, prefix: str) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return
        for child in children:
            rel = f"{prefix}{child.name}"
            if child.is_symlink():
                # A symlink is pinned by name and target whatever it points at.
                entries.append(PackEntry(rel, Path(child.path), True))
            elif child.is_dir(follow_symlinks=False):
                if child.name in IGNORED_DIRS:
                    continue
                walk(Path(child.path), f"{rel}/")
            elif child.is_file(follow_symlinks=False):
                if _ignored(child.name, Path(child.name).suffix):
                    continue
                entries.append(PackEntry(rel, Path(child.path), False))

    walk(pack_dir, "")
    return sorted(entries, key=lambda e: e.rel)


def iter_pack_files(pack_dir: Path) -> list[Path]:
    """Every regular content file in the pack, sorted by relative POSIX path.

    Symlinks are excluded here because callers of this function read bytes; use
    `iter_pack_entries` for anything that must account for the whole pack.
    """
    return [e.path for e in iter_pack_entries(pack_dir) if not e.is_symlink]


def pack_content_hash(pack_dir: Path) -> str:
    """sha256 over every pack entry, path-sorted.

    Files contribute (relative path, size, bytes); symlinks contribute
    (relative path, target string) under a distinguishing tag.
    """
    h = hashlib.sha256()
    for entry in iter_pack_entries(pack_dir):
        rel = entry.rel.encode()
        if entry.is_symlink:
            target = entry.target().encode()
            h.update(_SYMLINK_TAG)
            h.update(len(rel).to_bytes(4, "big"))
            h.update(rel)
            h.update(len(target).to_bytes(8, "big"))
            h.update(target)
            continue
        data = entry.path.read_bytes()
        h.update(len(rel).to_bytes(4, "big"))
        h.update(rel)
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


# (dir, stat signature) -> hash. Avoids re-reading every pack file on each
# method invocation while still noticing edits (mtime/size change).
_cache: dict[str, tuple[tuple, str]] = {}


def _stat_signature(pack_dir: Path) -> tuple:
    """Cheap change detector. `lstat` throughout: a symlink's own metadata and
    target decide whether it changed, never the metadata of what it points at —
    otherwise re-pointing a link between two same-size files would be invisible
    to the cache and the re-hash would never happen."""
    signature = []
    for entry in iter_pack_entries(pack_dir):
        try:
            stat = entry.path.lstat()
        except OSError:  # vanished mid-walk: force a re-hash rather than cache it
            signature.append((entry.rel, None, None, None))
            continue
        target = entry.target() if entry.is_symlink else None
        signature.append((entry.rel, stat.st_mtime_ns, stat.st_size, target))
    return tuple(signature)


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
    an older tret); callers should warn rather than fail so upgrades are not
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
            "content (restart tret, or POST /api/packs/install), or restore the original files."
        )
