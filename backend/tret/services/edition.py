"""UI edition resolution: which set of screens the web UI shows.

Purely presentational. An edition hides navigation and routes in the SPA; it
is not an access control and never gates an API call.
"""
from __future__ import annotations

EDITIONS = ("standard", "cohort")
DEFAULT_EDITION = "standard"
# The key inside `Workspace.settings` holding a per-workspace override.
EDITION_SETTINGS_KEY = "edition"


def resolve_edition(workspace_settings: dict | None, default: str) -> str:
    """The workspace's own override when it is a known edition, else the
    deployment default (an unknown or stale stored value is ignored, not
    an error)."""
    value = (workspace_settings or {}).get(EDITION_SETTINGS_KEY)
    return value if isinstance(value, str) and value in EDITIONS else default
