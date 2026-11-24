"""Confinement for the file paths a pack manifest names.

`pack.yaml` names files by path — `doctrine:` entries, a task's
`output_schema`, a dataset's `file`, a method's `entrypoint` — and every one
of those paths is author-controlled. An archive upload or a pack-builder
draft lets any workspace admin author one, and any user can be a workspace
admin (their own team workspace, or the personal workspace every
multi-tenant sign-up owns). So a manifest path must never be trusted to stay
inside the pack: `doctrine: ["/proc/self/environ"]` would otherwise be read
at install, hashed, and handed back verbatim by `GET /api/packs/{id}`, and
`doctrine: ["/dev/zero"]` would hang the reader.

`resolve_pack_file` is the one way to turn a manifest path into a file to
read. `validate_pack` uses it so a pack that escapes never installs, and
every runtime reader uses it too, so a pack row installed before this check
existed is refused at read time rather than trusted because it is already in
the database.

Deliberately free of heavy imports: `tret.engine.context` imports this, and
that module must stay importable in a core-only install.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class PackPathError(OSError):
    """A manifest path that is not a regular file inside its pack directory.

    An `OSError` on purpose: every runtime reader already treats an
    unreadable pack file as "unavailable" through its `except OSError`
    handling, and an escaping path must land in exactly that branch rather
    than surface as a 500.
    """


class PackFileMissing(PackPathError):
    """A well-formed, contained manifest path whose file does not exist."""


def pack_relpath_problem(rel: object) -> str | None:
    """Why `rel` can never name a file inside a pack, judged on the string
    alone (no filesystem access), or None if it is a plain relative path.

    Rejects: non-strings, empty strings, NUL bytes, backslashes, absolute
    paths, Windows drive prefixes, and any `..` segment.
    """
    if not isinstance(rel, str) or not rel.strip():
        return "path is empty"
    if "\x00" in rel:
        return "path contains a NUL byte"
    if "\\" in rel:
        return "path contains a backslash; use forward slashes"
    if _DRIVE_RE.match(rel):
        return "path is drive-rooted"
    posix = PurePosixPath(rel)
    if posix.is_absolute():
        return "path is absolute; pack paths are relative to the pack directory"
    if ".." in posix.parts:
        return "path contains '..'"
    return None


def resolve_pack_file(pack_dir: Path | str, rel: object) -> Path:
    """The resolved path of the regular file `rel` names inside `pack_dir`.

    Raises `PackPathError` when `rel` is not a plain relative path, when it
    resolves (symlinks followed) outside `pack_dir`, or when what it names is
    missing or is not a regular file (a directory, a FIFO, a device such as
    `/dev/zero`).
    """
    problem = pack_relpath_problem(rel)
    if problem is not None:
        raise PackPathError(f"{rel!r}: {problem}")
    try:
        root = Path(pack_dir).resolve()
        candidate = (root / str(rel)).resolve()
        inside = candidate.is_relative_to(root)
        regular = inside and candidate.is_file()
        exists = regular or (inside and candidate.exists())
    except (OSError, RuntimeError, ValueError) as e:
        # A name too long for the filesystem (OSError), a symlink loop
        # (RuntimeError from `resolve()` on 3.12), and the like. The cause's
        # own message is left out: it would carry the absolute server path.
        raise PackPathError(f"{rel!r}: cannot be resolved ({type(e).__name__})") from None
    if not inside:
        raise PackPathError(f"{rel!r}: resolves outside the pack directory")
    if not regular:
        if exists:
            raise PackPathError(f"{rel!r}: not a regular file")
        raise PackFileMissing(f"{rel!r}: file missing")
    return candidate
