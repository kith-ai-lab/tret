"""Rubrics for the batch evaluator (`tret/batches/evaluation.py`).

A rubric is plain data: weighted criteria with a 5 anchor and a 1 anchor, the
flags a judge may raise, and the quality a result needs to count as acceptable.
Two are built in (`disclosure-v1`, `prompt-default-v1`); a prompt batch may carry
its own, validated here. A rubric is frozen onto the batch (and again onto each
evaluation pass) when it is created, so a later change to the built-ins never
rewrites what a finished pass was judged against.

Pure module: no database, no engine. The evaluator tools and the scorecard
import it, so it must stay cheap to import.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Stamped on every evaluator run's task input (`evaluation.py`); `services/outcomes.py`
# reads it, so it lives in this dependency-free module.
EVALUATION_ID_KEY = "_batch_evaluation_id"
DISCLOSURE_RUBRIC_KEY = "disclosure-v1"
PROMPT_RUBRIC_KEY = "prompt-default-v1"
DEFAULT_ACCEPTABLE_QUALITY = 60
APPLIES = ("all", "written", "gap")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
MAX_CRITERIA, MIN_CRITERIA = 8, 2
MAX_FLAGS = 15
MAX_LABEL_CHARS = 60
MAX_ANCHOR_CHARS = 300
# Target classes (`tret/batches/scoring.py`) whose result is an honest "nothing to
# collect / nothing produced" rather than a delivered one.
GAP_CLASSES = frozenset({"gap", "rejected", "failed"})


class EvaluationIn(BaseModel):
    """The optional `evaluation` block of both create bodies and `start_batch`."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    judge_model: str | None = Field(default=None, max_length=200)
    budget_usd: float | None = Field(default=None, gt=0, le=100_000)
    rubric: dict[str, Any] | None = None


def _criterion(key: str, label: str, weight: int, applies: str, five: str, one: str) -> dict:
    return {"key": key, "label": label, "weight": weight, "applies": applies, "five": five, "one": one}


def rubric_version(rubric: dict) -> str:
    """First 12 hex digits of the sha256 of the rubric's canonical JSON, taken over
    everything except its own `key` and `version` (a custom rubric's key embeds
    its version)."""
    body = {k: v for k, v in rubric.items() if k not in ("key", "version")}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _finish(rubric: dict) -> dict:
    return {**rubric, "version": rubric_version(rubric)}


def _disclosure_v1() -> dict:
    return _finish(
        {
            "key": DISCLOSURE_RUBRIC_KEY,
            "title": "Disclosure collection",
            "acceptable_quality": DEFAULT_ACCEPTABLE_QUALITY,
            "criteria": [
                _criterion(
                    "right_document", "Right document", 30, "written",
                    "The issuer's FY primary sustainability disclosure for the requested fiscal year, "
                    "in the language of record (English where an English edition is published)",
                    "Another year, entity or language, or a summary or factsheet in place of the report",
                ),
                _criterion(
                    "coverage", "Coverage", 25, "written",
                    "The report plus its assurance statement and the incorporated or embedded sections",
                    "A required part is missing with no gap note",
                ),
                _criterion(
                    "page_map_precision", "Page-map precision", 20, "written",
                    "Retained pages are the disclosure; nothing material dropped, little extra",
                    "Mostly irrelevant pages, or the disclosure cut short",
                ),
                _criterion(
                    "provenance", "Provenance and doctrine", 15, "all",
                    "Issuer URL, hash and period labels present; substitutions logged; no inferred page ranges",
                    "An unlogged substitution or an invented page range",
                ),
                _criterion(
                    "gap_honesty", "Gap honesty", 10, "gap",
                    "The reason is specific and true, with the URLs tried listed",
                    "A vague or wrong reason",
                ),
            ],
            "flags": [
                {"key": "wrong_year", "label": "Wrong fiscal year"},
                {"key": "wrong_language", "label": "Wrong language"},
                {"key": "wrong_entity", "label": "Wrong entity"},
                {"key": "summary_not_report", "label": "Summary, not the report"},
                {"key": "missing_assurance", "label": "Assurance statement missing"},
                {"key": "over_broad_map", "label": "Page map too broad"},
                {"key": "under_scoped_map", "label": "Page map too narrow"},
                {"key": "missed_primary", "label": "Missed the primary document"},
                {"key": "unlogged_substitution", "label": "Unlogged substitution"},
                {"key": "gap_reason_wrong", "label": "Gap reason wrong"},
            ],
        }
    )


def _prompt_default_v1() -> dict:
    return _finish(
        {
            "key": PROMPT_RUBRIC_KEY,
            "title": "Prompt batch",
            "acceptable_quality": DEFAULT_ACCEPTABLE_QUALITY,
            "criteria": [
                _criterion(
                    "correctness", "Correctness", 35, "all",
                    "Right for this row, consistent with its inputs and any documents",
                    "Wrong, or contradicts the inputs",
                ),
                _criterion(
                    "completeness", "Completeness", 25, "all",
                    "Answers every part the task asked for",
                    "Misses most of what was asked",
                ),
                _criterion(
                    "instructions", "Instruction following", 20, "all",
                    "Follows the required format and constraints",
                    "Ignores the required format",
                ),
                _criterion(
                    "grounding", "Grounding", 20, "all",
                    "Claims are supported by the row or cited sources, uncertainty stated",
                    "Unsupported or invented claims",
                ),
            ],
            "flags": [
                {"key": "wrong_answer", "label": "Wrong answer"},
                {"key": "incomplete", "label": "Incomplete"},
                {"key": "format_violation", "label": "Format violation"},
                {"key": "unsupported_claim", "label": "Unsupported claim"},
                {"key": "refused_without_reason", "label": "Refused without reason"},
            ],
        }
    )


def default_rubric(profile: str) -> dict:
    """The built-in rubric for a profile; `KeyError` for a profile that has none."""
    if profile == "disclosure":
        return _disclosure_v1()
    if profile == "prompt":
        return _prompt_default_v1()
    raise KeyError(profile)


class RubricError(ValueError):
    """A supplied rubric that cannot be accepted; the message is the 422 detail."""


def validate_custom_rubric(raw: Any) -> dict:
    """A prompt batch's own rubric, normalised: 2-8 criteria with unique keys,
    integer weights 1-100, bounded anchors, at most 15 flags. Its key is
    `prompt-custom-<version>`, so two different custom rubrics never share a key
    (the history that expected scores are drawn from is per rubric key)."""
    if not isinstance(raw, dict):
        raise RubricError("rubric must be an object")
    criteria_raw = raw.get("criteria")
    if not isinstance(criteria_raw, list) or not MIN_CRITERIA <= len(criteria_raw) <= MAX_CRITERIA:
        raise RubricError(f"rubric.criteria must list {MIN_CRITERIA}-{MAX_CRITERIA} criteria")
    criteria: list[dict] = []
    seen: set[str] = set()
    for i, c in enumerate(criteria_raw, start=1):
        where = f"rubric.criteria[{i}]"
        if not isinstance(c, dict):
            raise RubricError(f"{where} must be an object")
        key = c.get("key")
        if not isinstance(key, str) or not KEY_RE.match(key):
            raise RubricError(f"{where}.key must match {KEY_RE.pattern}")
        if key in seen:
            raise RubricError(f"{where}.key '{key}' is used twice")
        seen.add(key)
        label = c.get("label")
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > MAX_LABEL_CHARS:
            raise RubricError(f"{where}.label must be 1-{MAX_LABEL_CHARS} characters")
        weight = c.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, int) or not 1 <= weight <= 100:
            raise RubricError(f"{where}.weight must be an integer from 1 to 100")
        applies = c.get("applies", "all")
        if applies not in APPLIES:
            raise RubricError(f"{where}.applies must be one of {', '.join(APPLIES)}")
        anchors = {}
        for side in ("five", "one"):
            text = c.get(side)
            if not isinstance(text, str) or not text.strip() or len(text.strip()) > MAX_ANCHOR_CHARS:
                raise RubricError(f"{where}.{side} must be 1-{MAX_ANCHOR_CHARS} characters")
            anchors[side] = text.strip()
        criteria.append(
            _criterion(key, label.strip(), weight, applies, anchors["five"], anchors["one"])
        )
    flags_raw = raw.get("flags", [])
    if not isinstance(flags_raw, list) or len(flags_raw) > MAX_FLAGS:
        raise RubricError(f"rubric.flags must be a list of at most {MAX_FLAGS} flags")
    flags: list[dict] = []
    flag_keys: set[str] = set()
    for i, f in enumerate(flags_raw, start=1):
        where = f"rubric.flags[{i}]"
        if not isinstance(f, dict):
            raise RubricError(f"{where} must be an object")
        key = f.get("key")
        if not isinstance(key, str) or not KEY_RE.match(key):
            raise RubricError(f"{where}.key must match {KEY_RE.pattern}")
        if key in flag_keys:
            raise RubricError(f"{where}.key '{key}' is used twice")
        flag_keys.add(key)
        label = f.get("label", key)
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > MAX_LABEL_CHARS:
            raise RubricError(f"{where}.label must be 1-{MAX_LABEL_CHARS} characters")
        flags.append({"key": key, "label": label.strip()})
    acceptable = raw.get("acceptable_quality", DEFAULT_ACCEPTABLE_QUALITY)
    if isinstance(acceptable, bool) or not isinstance(acceptable, (int, float)) or not 0 <= acceptable <= 100:
        raise RubricError("rubric.acceptable_quality must be a number from 0 to 100")
    title = raw.get("title", "Custom rubric")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 100:
        raise RubricError("rubric.title must be 1-100 characters")
    body = {
        "title": title.strip(),
        "acceptable_quality": acceptable,
        "criteria": criteria,
        "flags": flags,
    }
    version = rubric_version(body)
    return {"key": f"prompt-custom-{version}", **body, "version": version}


def criterion_applies(applies: str, row_class: str | None) -> bool:
    """Does a criterion apply to a target of this class? `written` = a delivered
    result, `gap` = an honest non-result, `all` = always."""
    if applies == "all":
        return True
    if applies == "written":
        return row_class == "success"
    if applies == "gap":
        return row_class in GAP_CLASSES
    return False


def compute_quality(rubric: dict, scores: dict, row_class: str | None) -> float | None:
    """0-100 quality: each non-null score s becomes (s - 1) / 4 x 100, averaged
    with the criterion weights renormalised over the criteria that apply to this
    class and were scored. None when no applicable criterion has a score."""
    total = weight_sum = 0.0
    for c in rubric.get("criteria") or []:
        if not criterion_applies(c.get("applies", "all"), row_class):
            continue
        entry = scores.get(c["key"])
        score = entry.get("score") if isinstance(entry, dict) else None
        if score is None:
            continue
        weight_sum += float(c["weight"])
        total += float(c["weight"]) * (float(score) - 1.0) / 4.0 * 100.0
    if weight_sum <= 0:
        return None
    return round(total / weight_sum, 2)


def rubric_text(rubric: dict, row_class: str | None = None) -> str:
    """The rubric as the judge reads it: criteria with weights, applicability and
    anchors, then the flags. With `row_class` it also says which criteria apply."""
    lines = [
        f"RUBRIC {rubric.get('key')} (acceptable quality: {rubric.get('acceptable_quality')} of 100)",
        "Score each criterion 1-5 (5 and 1 are defined below; 3 is a usable result with a clear "
        "shortcoming) or null when it does not apply.",
        "",
    ]
    for c in rubric.get("criteria") or []:
        applies = c.get("applies", "all")
        note = ""
        if row_class is not None:
            note = " [applies to this result]" if criterion_applies(applies, row_class) else " [does not apply: score null]"
        lines.append(f"- {c['key']} ({c['label']}, weight {c['weight']}, applies to: {applies}){note}")
        lines.append(f"    5: {c['five']}")
        lines.append(f"    1: {c['one']}")
    flags = rubric.get("flags") or []
    lines += ["", "FLAGS (use only these keys, only when the evidence shows them):"]
    lines += [f"- {f['key']}: {f['label']}" for f in flags] or ["- (none)"]
    return "\n".join(lines)


__all__ = [
    "DISCLOSURE_RUBRIC_KEY",
    "PROMPT_RUBRIC_KEY",
    "EvaluationIn",
    "RubricError",
    "compute_quality",
    "criterion_applies",
    "default_rubric",
    "rubric_text",
    "rubric_version",
    "validate_custom_rubric",
]
