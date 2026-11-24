"""The record `ModelRouter.route_jury` returns: who sits on a jury, why, and what else was
considered. Plain data, JSON round-trippable, no router dependency (the freeze stores it)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Bump when the way jurors are ranked or picked changes; recorded on every selection.
JURY_RANKING_VERSION = "jury-v1"


@dataclass
class JuryPick:
    model: str
    family: str
    rank: int  # 1-based position in the policy's eligible, ordered list
    evidence: dict
    why: str
    alternatives: list[dict] = field(default_factory=list)  # next-best of the same family


@dataclass
class JurySelection:
    purpose: str
    k: int
    distinct_authors: bool
    policy: dict
    objective: str
    open_weights: str
    min_context_window: int
    task_shape: str
    task_family: str | None
    ranking_version: str
    router_version: str
    priors_version: str
    exploration: str  # always "off": a pass is never explored
    llm_call: bool  # always False: no router LLM call, no model spend
    eligible: int
    families_eligible: int
    picks: list[JuryPick]
    skipped_families: list[dict]
    excluded_models: list[dict]
    created_at: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def models(self) -> list[str]:
        return [p.model for p in self.picks]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "JurySelection":
        d = dict(data)
        d["picks"] = [JuryPick(**p) for p in d.get("picks", [])]
        return cls(**d)
