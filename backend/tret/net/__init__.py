"""tret's outbound network boundary.

    tret.net.policy   what each class of destination is allowed to reach
    tret.net.guard    the per-URL checks (scheme, allowlist, private address)
    tret.net.client   the only HTTP client factory in the codebase
    tret.net.audit    what was reached, and what was refused
    tret.net.search   web search, behind a provider interface (default: none)
    tret.net.fetch    URL -> snapshot -> Document, the research evidence path

Read `policy.py` first: it explains why egress is five switchable classes
rather than one boolean, and why every rule in this package narrows.
"""
from tret.net.client import build_client, open_client
from tret.net.guard import check_url
from tret.net.policy import (
    CLASS_CATALOG,
    CLASS_LOCAL,
    CLASS_PROVIDER,
    CLASS_RESEARCH,
    CLASS_SEARCH,
    CLASS_TELEMETRY,
    EGRESS_CLASSES,
    MODE_OFF,
    MODE_ON,
    MODE_REPLAY,
    EgressDenied,
    effective_mode,
    egress_status,
    log_egress_at_boot,
    policy_for,
)

__all__ = [
    "CLASS_CATALOG",
    "CLASS_LOCAL",
    "CLASS_PROVIDER",
    "CLASS_RESEARCH",
    "CLASS_SEARCH",
    "CLASS_TELEMETRY",
    "EGRESS_CLASSES",
    "MODE_OFF",
    "MODE_ON",
    "MODE_REPLAY",
    "EgressDenied",
    "build_client",
    "check_url",
    "effective_mode",
    "egress_status",
    "log_egress_at_boot",
    "open_client",
    "policy_for",
]
