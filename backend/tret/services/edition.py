"""UI edition resolution: which set of screens the web UI shows.

Mostly presentational: an edition hides navigation and routes in the SPA and is
not an access control. The cohort edition also changes two pieces of behaviour
(Learning round): every folder attached to a session is a write target for
saving files (create-only, into a `tret/` subfolder; `services/connections.py::
session_write_targets`), and the unvetted-analysis lane may be switched on
(`TRET_UNVETTED_ANALYSIS`). The standard edition is unchanged.
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


def default_export_theme(workspace_settings: dict | None, default_edition: str) -> str:
    """The document export style used when none is named: "report" in the
    cohort edition, "plain" everywhere else (existing deployments unchanged)."""
    return "report" if resolve_edition(workspace_settings, default_edition) == "cohort" else "plain"
