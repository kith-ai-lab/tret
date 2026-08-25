"""Where an archive-installed pack's extracted directory lives on disk.

Today there is exactly one implementation — straight to local disk under
`TRET_STORAGE_DIR/packs/{pack_id}/`, reusing `get_settings().storage_dir`
(the same setting `api/documents.py` writes uploads under; no new setting was
needed for this). `PackStorage` exists as a seam so a later object-storage
backend (S3, GCS, ...) can slot in behind the same three calls —
`path_for`/`staging_dir`/`remove` — without `loader.py` or the delete
endpoint needing to change. One directory per `Pack` row: no cross-workspace
sharing or content-addressed dedup yet (see the plan's Phase A note); that is
documented future work, not a gap this seam hides.

Path-installed packs (`POST /api/packs/install`, an operator-supplied
filesystem path anywhere on the server) never go through this seam — their
`source_path` is wherever the operator pointed it, which is exactly why
`owns()` exists: it is the one check standing between "delete this
archive-installed pack's directory" and "rmtree an operator's own directory
because a UUID happened to collide with a path".
"""
from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from tret.config import get_settings

log = logging.getLogger("tret.packs.storage")


class PackStorage:
    """Filesystem-backed pack storage, rooted at `storage_dir/packs/`."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or (Path(get_settings().storage_dir).resolve() / "packs")

    def path_for(self, pack_id: uuid.UUID) -> Path:
        """The directory a given pack's extracted content lives (or will live)
        in. Does not create it — callers extract or rename into place."""
        return self.root / str(pack_id)

    def staging_dir(self) -> Path:
        """A fresh, uncommitted directory to extract an archive into before the
        installing pack's id is known (or before install has validated it)."""
        return self.root / f".staging-{uuid.uuid4().hex}"

    def remove(self, path: Path) -> None:
        """Delete the pack directory at `path`. A no-op if it is already gone.

        Takes the path itself rather than a pack id so the caller passes the
        exact same path `owns()` just validated — deriving it fresh from
        `pack_id` here would let the check and the action silently drift
        apart (e.g. `path_for` changing) without either one noticing.

        Never raises: a failed removal (permissions, a file busy on the OS)
        leaks a directory rather than the caller's own cleanup (e.g.
        `DELETE /api/packs/{id}`, which has already committed the row gone by
        the time this runs) — a leaked directory is a cheap, logged loose
        end. Logged via `rmtree`'s own `onerror` rather than swallowed
        silently (bare `ignore_errors=True`, the previous behavior), so a
        failure is at least discoverable.
        """

        if not path.exists():
            return

        def _log_failure(_func, failed_path, exc: BaseException) -> None:
            log.warning("failed to remove %s: %s", failed_path, exc)

        # `onexc` (not the deprecated `onerror`) — this project's floor is
        # Python 3.12, which has it.
        shutil.rmtree(path, onexc=_log_failure)

    def owns(self, source_path: str | None) -> bool:
        """Whether `source_path` names a directory this seam manages — i.e.
        whether it is safe to `remove()`. False for anything else, including
        an unreadable/unresolvable path, on the same fail-closed logic as
        every other "when in doubt, don't touch it" check in this codebase.
        """
        if not source_path:
            return False
        try:
            resolved = Path(source_path).resolve()
        except OSError:
            return False
        return resolved.parent == self.root.resolve()


def get_pack_storage() -> PackStorage:
    return PackStorage()
