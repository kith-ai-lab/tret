"""Guardrail settings: what a member may tune, what is locked, what the operator owns (R25).

Three tiers, mirroring Ben's request (2026-11-20):

- **locked** — always on, no switch anywhere: numbers only via lookups or vetted
  methods (the unvetted lane is labelled, never silent), provenance on every run,
  draft-until-approved, create-only file writes.
- **member** — adjustable per workspace (``Workspace.settings["guardrails"]``):
  the prose number check, how many rewrites it may ask for (0-3), the HTML
  check, web research (can only narrow what the operator allows), a per-run
  spend cap (can only narrow the harness cap), and the subagent width per call
  (can only narrow ``TRET_MAX_FANOUT``).
- **operator** — instance settings members can see but not change: code
  sandbox network isolation, pack integrity, the unvetted lane, the research
  allowlist, the degenerate-output guard, the output-token limit.

``resolve_guardrails`` is the single place the harness reads member settings
from, and ``snapshot`` is what gets written to ``runs.guardrails["settings"]``
so a run's audit trail shows what was on.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from tret.config import Settings

#: Rewrites the prose number check may ask for before a reply ships flagged
#: ``unresolved``. 2 is the historical behaviour (GROUNDING_MAX_REPAIRS = 3
#: replies in total: the first plus two rewrites).
DEFAULT_REPAIR_ATTEMPTS = 2
MAX_REPAIR_ATTEMPTS = 3


@dataclass(frozen=True)
class GuardrailConfig:
    """Effective member-tier guardrails for one run (already clamped to what the
    operator allows)."""

    number_check: bool = True
    repair_attempts: int = DEFAULT_REPAIR_ATTEMPTS
    html_check: bool = True
    web_research: bool = False
    # Per-run spend cap requested by the member, in USD. None = the harness cap
    # applies unchanged. The harness takes min(harness cap, this) — narrow only.
    spend_cap_usd: float | None = None
    # Max subagents per delegation call. None = the instance's TRET_MAX_FANOUT;
    # 0 = no delegation at all. Always <= TRET_MAX_FANOUT.
    max_subagents: int | None = None


def _as_bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _research_allowed(settings: Settings) -> bool:
    return str(settings.egress).lower() == "on" and str(settings.egress_research).lower() in (
        "on",
        "replay",
    )


def resolve_guardrails(settings: Settings, workspace_settings: dict | None) -> GuardrailConfig:
    """Member settings stored on the workspace, clamped to operator limits.

    Missing or malformed keys fall back to the defaults, so a workspace that
    never opened the panel behaves exactly as before this module existed.
    """
    raw = (workspace_settings or {}).get("guardrails") or {}
    if not isinstance(raw, dict):
        raw = {}

    repairs = raw.get("repair_attempts", DEFAULT_REPAIR_ATTEMPTS)
    if not isinstance(repairs, int) or isinstance(repairs, bool):
        repairs = DEFAULT_REPAIR_ATTEMPTS
    repairs = max(0, min(MAX_REPAIR_ATTEMPTS, repairs))

    research_allowed = _research_allowed(settings)
    web_research = research_allowed and _as_bool(raw.get("web_research"), True)

    cap = raw.get("spend_cap_usd")
    spend_cap: float | None = None
    if isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap > 0:
        spend_cap = float(cap)

    # TRET_MAX_FANOUT <= 0 means unlimited (as `delegate_parallel` reads it): the
    # member's value is then kept as given, never clamped down to 0.
    unlimited_fanout = int(settings.max_fanout) <= 0
    fanout_limit = max(0, int(settings.max_fanout))
    subagents = raw.get("max_subagents")
    max_subagents: int | None = None
    if isinstance(subagents, int) and not isinstance(subagents, bool):
        max_subagents = max(0, subagents if unlimited_fanout else min(fanout_limit, subagents))

    return GuardrailConfig(
        number_check=_as_bool(raw.get("number_check"), True),
        repair_attempts=repairs,
        html_check=_as_bool(raw.get("html_check"), True),
        web_research=web_research,
        spend_cap_usd=spend_cap,
        max_subagents=max_subagents,
    )


def operator_state(settings: Settings) -> dict[str, Any]:
    """Instance-level guardrail state members can see but not change."""
    hosts = [h for h in (settings.egress_research_allow_hosts or "").replace(";", ",").split(",") if h.strip()]
    return {
        "code_sandbox_network_isolation": bool(settings.methods_network_isolation),
        "pack_integrity": True,
        "unvetted_analysis": bool(getattr(settings, "unvetted_analysis", False)),
        "web_research_allowed": _research_allowed(settings),
        "research_allowlist_hosts": len(hosts),
        "degenerate_output_guard": True,
        "max_fanout": int(settings.max_fanout),
    }


def snapshot(cfg: GuardrailConfig, settings: Settings) -> dict[str, Any]:
    """What goes in ``runs.guardrails["settings"]``: member values, operator
    state, and the locked rules by name, so the audit trail shows all three."""
    return {
        "member": asdict(cfg),
        "operator": operator_state(settings),
        "locked": [
            "numbers_from_lookups_or_methods",
            "provenance",
            "draft_until_approved",
            "create_only_writes",
        ],
    }


# ── API catalog (R25) ────────────────────────────────────────────────────────
#: The chat harness's seeded output limit when no harness row says otherwise.
DEFAULT_OUTPUT_LIMIT = 16384
#: Window the panel's per-item overhead covers.
OVERHEAD_WINDOW_DAYS = 30
SPEND_CAP_MIN_USD = 0.01
SPEND_CAP_MAX_USD = 10_000.0

TIERS: list[dict[str, str]] = [
    {
        "id": "locked",
        "title": "Always on",
        "description": "Rules that hold in every run. There is no switch for them anywhere.",
    },
    {
        "id": "member",
        "title": "You can adjust",
        "description": "Checks and limits a workspace admin can tune. They can only be made "
        "stricter than the operator's limits, never looser.",
    },
    {
        "id": "operator",
        "title": "Set by the operator",
        "description": "Instance-level settings you can see here but not change.",
    },
]


class GuardrailValidationError(ValueError):
    """A bad member-settings update; ``errors`` are the 422 messages."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


MEMBER_KEYS = (
    "number_check",
    "repair_attempts",
    "html_check",
    "web_research",
    "spend_cap_usd",
    "max_subagents",
)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_update(body: Any, settings: Settings) -> dict[str, Any]:
    """Validate a partial member-settings update and clamp it to what is allowed.

    Returns only the keys present in ``body``. ``None`` for ``spend_cap_usd`` or
    ``max_subagents`` means "clear it" (back to the harness cap / the instance
    fan-out limit). Unknown keys and wrong types raise
    ``GuardrailValidationError``; out-of-range numbers are clamped.
    """
    if not isinstance(body, dict):
        raise GuardrailValidationError(["Body must be a JSON object."])
    errors: list[str] = []
    out: dict[str, Any] = {}
    for key in body:
        if key not in MEMBER_KEYS:
            errors.append(f"Unknown guardrail setting: {key!r}.")
    fanout = max(0, int(settings.max_fanout))
    for key in ("number_check", "html_check", "web_research"):
        if key in body:
            if isinstance(body[key], bool):
                out[key] = body[key]
            else:
                errors.append(f"{key} must be true or false.")
    if "repair_attempts" in body:
        v = body["repair_attempts"]
        if _is_int(v):
            out["repair_attempts"] = max(0, min(MAX_REPAIR_ATTEMPTS, v))
        else:
            errors.append(f"repair_attempts must be a whole number from 0 to {MAX_REPAIR_ATTEMPTS}.")
    if "spend_cap_usd" in body:
        v = body["spend_cap_usd"]
        if v is None:
            out["spend_cap_usd"] = None
        elif _is_number(v) and v == v and v > 0:
            out["spend_cap_usd"] = float(max(SPEND_CAP_MIN_USD, min(SPEND_CAP_MAX_USD, v)))
        else:
            errors.append("spend_cap_usd must be a number above 0, or null for no cap.")
    if "max_subagents" in body:
        v = body["max_subagents"]
        if v is None:
            out["max_subagents"] = None
        elif _is_int(v):
            out["max_subagents"] = max(0, v if int(settings.max_fanout) <= 0 else min(fanout, v))
        else:
            upper = fanout if int(settings.max_fanout) > 0 else "any limit you like"
            errors.append(f"max_subagents must be a whole number from 0 to {upper}, or null.")
    if errors:
        raise GuardrailValidationError(errors)
    return out


def apply_update(workspace_settings: dict | None, update: dict[str, Any]) -> dict[str, Any]:
    """New stored ``guardrails`` dict: the existing one with ``update`` merged in
    (``None`` values remove a key). A fresh dict, so the caller can reassign it."""
    raw = (workspace_settings or {}).get("guardrails")
    stored = dict(raw) if isinstance(raw, dict) else {}
    for key, value in update.items():
        if value is None:
            stored.pop(key, None)
        else:
            stored[key] = value
    return stored


def aggregate_overhead(rows: list[dict[str, Any]], window_days: int = OVERHEAD_WINDOW_DAYS) -> dict | None:
    """Repair overhead over a window of runs.

    Each row: ``{"guardrails": runs.guardrails, "input_tokens", "output_tokens",
    "cost_usd", "energy_wh"}``. Cost and energy are pro-rated per run by the
    share of its tokens spent on repairs. ``runs`` counts the runs that carry a
    guardrails record. ``None`` when no run does (nothing to show).
    """
    runs = 0
    turns = tin = tout = 0
    cost = energy = 0.0
    for row in rows:
        g = row.get("guardrails")
        if not isinstance(g, dict):
            continue
        runs += 1
        oh = g.get("overhead")
        if not isinstance(oh, dict):
            continue
        rt = int(oh.get("repair_turns") or 0)
        ri = int(oh.get("repair_input_tokens") or 0)
        ro = int(oh.get("repair_output_tokens") or 0)
        turns += rt
        tin += ri
        tout += ro
        total = int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)
        if total > 0 and (ri + ro) > 0:
            share = min(1.0, (ri + ro) / total)
            cost += float(row.get("cost_usd") or 0) * share
            energy += float(row.get("energy_wh") or 0) * share
    if runs == 0:
        return None
    return {
        "window_days": window_days,
        "runs": runs,
        "repair_turns": turns,
        "input_tokens": tin,
        "output_tokens": tout,
        "cost_usd": round(cost, 6),
        "energy_wh": round(energy, 6),
    }


def _item(
    id_: str,
    tier: str,
    title: str,
    description: str,
    *,
    value: Any,
    state: str,
    adjustable: bool = False,
    bounds: dict | None = None,
    overhead: dict | None = None,
) -> dict[str, Any]:
    return {
        "id": id_,
        "tier": tier,
        "title": title,
        "description": description,
        "adjustable": adjustable,
        "value": value,
        "state": state,
        "bounds": bounds,
        "overhead": overhead,
    }


def build_catalog(
    settings: Settings,
    workspace_settings: dict | None,
    *,
    overhead: dict | None = None,
    output_limit: int | None = None,
) -> dict[str, Any]:
    """The ``GET /api/guardrails`` payload: tiers, every item with its current
    value and state, and the effective member config."""
    cfg = resolve_guardrails(settings, workspace_settings)
    op = operator_state(settings)
    fanout = int(op["max_fanout"])
    # One repair overhead figure covers both checks: a repair turn is a repair
    # turn whichever check asked for it.
    shared = dict(overhead, shared_with="number_check+html_check") if overhead else None
    on_off = lambda b: "on" if b else "off"  # noqa: E731

    locked = [
        _item("numbers_from_lookups_or_methods", "locked", "Numbers come from lookups or vetted methods",
              "Figures in a deliverable must trace to a dataset lookup, a vetted method run or something "
              "you supplied. Figures from the unvetted lane are always labelled unvetted.",
              value=True, state="always on"),
        _item("provenance", "locked", "Provenance on every run",
              "Every run records what it read, which models answered, what it cost and its energy estimate.",
              value=True, state="always on"),
        _item("draft_until_approved", "locked", "Drafts stay drafts until approved",
              "Findings and drafted sections are not final until a person (or the workspace's "
              "auto-approval policy, which is logged) approves them.",
              value=True, state="always on"),
        _item("create_only_writes", "locked", "File writes never overwrite",
              "Saving to a connected folder creates a new file; an existing file is never replaced.",
              value=True, state="always on"),
    ]
    member = [
        _item("number_check", "member", "Number check",
              "Checks every figure in a reply against what the run actually retrieved or you wrote, "
              "and asks the model to rewrite the reply if one does not trace.",
              value=cfg.number_check, state=on_off(cfg.number_check), adjustable=True,
              overhead=shared),
        _item("repair_attempts", "member", "Rewrites allowed",
              "How many times the model may be asked to rewrite a reply after a check fails. "
              "0 means the reply ships flagged instead.",
              value=cfg.repair_attempts, state=str(cfg.repair_attempts), adjustable=True,
              bounds={"min": 0, "max": MAX_REPAIR_ATTEMPTS}),
        _item("html_check", "member", "HTML check",
              "Checks HTML in a reply for unclosed tags, stray table cells and cut-off documents, "
              "and asks the model to reissue the file once if it finds any.",
              value=cfg.html_check, state=on_off(cfg.html_check), adjustable=True,
              overhead=shared),
        _item("web_research", "member", "Web research",
              "Lets the model search the web and fetch pages from the operator's allowlist. "
              "You can turn it off; you cannot turn on what the operator has not allowed.",
              value=cfg.web_research,
              state=on_off(cfg.web_research) if op["web_research_allowed"] else "not allowed by the operator",
              adjustable=bool(op["web_research_allowed"])),
        _item("spend_cap_usd", "member", "Spend cap per run (USD)",
              "A lower ceiling on what one run (including its subagents) may spend. It can only "
              "tighten the harness cap, never raise it.",
              value=cfg.spend_cap_usd,
              state="harness cap" if cfg.spend_cap_usd is None else f"${cfg.spend_cap_usd:g} per run",
              adjustable=True, bounds={"min": SPEND_CAP_MIN_USD, "max": SPEND_CAP_MAX_USD}),
        _item("max_subagents", "member", "Subagents per call",
              "The most subagents one delegation call may start. 0 turns delegation off.",
              value=(None if fanout <= 0 else fanout) if cfg.max_subagents is None else cfg.max_subagents,
              state=(("no limit" if fanout <= 0 else "instance limit")
                     if cfg.max_subagents is None else str(cfg.max_subagents)),
              adjustable=True, bounds={"min": 0, "max": fanout if fanout > 0 else None}),
    ]
    operator = [
        _item("code_sandbox_network_isolation", "operator", "Code sandbox network isolation",
              "Methods and analysis code run with no network access when the host supports it.",
              value=op["code_sandbox_network_isolation"], state=on_off(op["code_sandbox_network_isolation"])),
        _item("pack_integrity", "operator", "Pack integrity",
              "Packs are verified against their recorded hashes before their methods run.",
              value=op["pack_integrity"], state=on_off(op["pack_integrity"])),
        _item("unvetted_analysis", "operator", "Unvetted analysis lane",
              "Whether the model may run its own calculations (cohort edition only). Results are "
              "always labelled unvetted and never feed verdict citations.",
              value=op["unvetted_analysis"], state=on_off(op["unvetted_analysis"])),
        _item("research_allowlist_hosts", "operator", "Research allowlist",
              "How many web hosts research tools may reach.",
              value=op["research_allowlist_hosts"], state=f"{op['research_allowlist_hosts']} hosts"),
        _item("degenerate_output_guard", "operator", "Degenerate-output guard",
              "Stops a model that loops on the same token, retries once on another model, and "
              "never reports such a run as completed.",
              value=op["degenerate_output_guard"], state=on_off(op["degenerate_output_guard"])),
        _item("output_limit", "operator", "Reply length limit",
              "The most tokens the chat model may write in one turn; a reply that hits it is "
              "marked cut off.",
              value=int(output_limit or DEFAULT_OUTPUT_LIMIT),
              state=f"{int(output_limit or DEFAULT_OUTPUT_LIMIT)} tokens"),
    ]
    return {
        "tiers": [dict(t) for t in TIERS],
        "items": locked + member + operator,
        "member": asdict(cfg),
    }
