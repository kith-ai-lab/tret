"""Batch profiles: what differs between one kind of batch and another.

The orchestrator in `tret/batches/core.py` is generic: it creates target rows,
launches one engine run per target (slot-limited), re-derives each target's
status from facts, and finalizes the batch. Everything that is specific to a
kind of batch lives behind `BatchProfile`: how a register is parsed, what the
run is asked to do, how a finished run is judged, what is written afterwards
and what the run-level records say. `disclosure` is the first profile (the
Kith Climate disclosure collection); others register themselves with
`register_profile`.

This module must not import `core` at module level (the built-in profiles do).
"""
from __future__ import annotations

import importlib
from typing import Any, Iterable, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_PROFILE = "disclosure"


class RegisterError(ValueError):
    """A register row that cannot be accepted; the message names the row."""


class BatchProfile(Protocol):
    slug: str
    # What a `Finding.schema_slug` must be for the finding to be one this
    # profile reviews; None = the profile has no review step. A profile whose
    # schema depends on the batch also defines `review_schema_for(batch)`, which
    # core prefers (`core.review_schema_of`).
    review_schema: str | None
    # `start` answers 409 with this when no harness can be found.
    no_harness_error: str
    # `_launch` pauses the batch with this as `last_error` when no harness is found.
    missing_harness_message: str

    async def parse_targets(self, body: Any) -> tuple[list[dict], list[str]]:
        """The create request's register as target rows (each with at least
        `position`, `slug`, `label`, `task_input`) plus warnings. Raises
        `RegisterError`, answered as 422."""

    def review_schema_for(self, batch: Any) -> str | None:
        """The finding schema that batch's targets are reviewed on; None = no
        review. `batch` may be None (the profile's default)."""

    async def validate_settings(self, db: AsyncSession, workspace_id: Any, body: Any) -> dict:
        """The settings dict stored on the batch; raises HTTPException."""

    async def harness_for(self, db: AsyncSession, batch: Any) -> Any:
        """The harness the batch's runs execute on, or None."""

    def task_input(self, batch: Any, target: Any, total: int) -> dict:
        """`{"message": ..., "_label": ...}`; core adds the batch/target keys
        and the model/iteration overrides."""

    async def settle_outcome(
        self, db: AsyncSession, batch: Any, target: Any, run: Any, findings: list[Any]
    ) -> dict | None:
        """Called by `_settle_target` before `derive_status`. A dict becomes
        `target.outcome`; None leaves it alone (a profile whose outcome comes
        from a run tool, like disclosure's, returns None)."""

    def derive_status(
        self,
        run_status: str | None,
        outcome: dict | None,
        findings: Iterable[Any],
        *,
        run_error: str | None = None,
        auto_write: bool = False,
        written: bool = False,
    ) -> tuple[str, str | None]:
        """(status, error) for a target; may return core's WRITE_PENDING."""

    async def write_outputs(
        self, db: AsyncSession, batch: Any, target: Any, findings: list[Any]
    ) -> tuple[str, str | None]:
        """Called when `derive_status` returned WRITE_PENDING."""

    def all_written(self, target: Any, findings: list[Any]) -> bool: ...

    async def after_approve(self, db: AsyncSession, finding: Any) -> None:
        """A review finding that belongs to one of this profile's targets was approved."""

    def target_extra(self, target: Any, run: Any, findings: list[Any]) -> dict:
        """Extra keys merged into the target's API JSON."""

    def counts_extra(self, targets: list[Any]) -> dict:
        """Extra keys merged into the batch's `counts`."""

    def settings_keys(self) -> tuple[str, ...]:
        """The keys of `batch.settings` the API echoes."""

    async def records(self, db: AsyncSession, batch: Any, *, finalized_at: Any = None) -> dict: ...

    def record_files(self, records: dict) -> dict[str, tuple[bytes, str]]:
        """{file name: (bytes, content type)} in upload order."""

    def finalize_blockers(self, targets: list[Any], findings_by_run: dict) -> list[str]:
        """Reasons the batch cannot be finalized yet (empty = none)."""


_REGISTRY: dict[str, BatchProfile] = {}
# Built-ins are imported lazily: they import `core`, which imports this module.
_BUILTIN = {
    "disclosure": "tret.batches.profiles.disclosure",
    "prompt": "tret.batches.profiles.prompt",
}


def register_profile(profile: BatchProfile) -> None:
    _REGISTRY[profile.slug] = profile


def get_profile(slug: str) -> BatchProfile:
    """The profile for `slug`; `KeyError` when unknown."""
    if slug not in _REGISTRY and slug in _BUILTIN:
        importlib.import_module(_BUILTIN[slug])  # registers itself
    return _REGISTRY[slug]


def profile_for(batch: Any) -> BatchProfile:
    return get_profile(getattr(batch, "profile", None) or DEFAULT_PROFILE)
