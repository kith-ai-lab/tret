"""Shared pieces of the evaluator's evidence pack (`evaluation_evidence` on the
batch profiles).

The pack is what the judge sees of one finished run, and it is blind: it never
carries the model that produced the run, its provider, its spend, tokens, energy,
iteration count or routing. Everything taken from the run itself (its tool-call
trail, its final message, its error) goes through `scrub`, which also removes
the run's own model and provider names wherever they happen to appear.
"""
from __future__ import annotations

import json
import re
from typing import Any

PACK_MAX_CHARS = 24_000
DIGEST_MAX_LINES = 60
DIGEST_LINE_CHARS = 160
FINAL_MESSAGE_CHARS = 2_000
MAX_ATTACHED_DOCUMENTS = 8
REDACTED = "[redacted]"
_KEY_ARGUMENTS = ("url", "query", "document_id", "stem", "path", "filename", "name")
_PROVIDER_TAG_RE = re.compile(r"\[[a-z0-9_-]{2,30}\]\s*")


def _identities(run: Any) -> list[str]:
    """Strings that identify the model or provider behind `run`, longest first."""
    names: set[str] = set()
    for value in (getattr(run, "model_used", None), getattr(run, "provider_used", None)):
        if isinstance(value, str) and value.strip():
            names.add(value.strip())
            tail = value.strip().rsplit("/", 1)[-1]
            if len(tail) >= 5:
                names.add(tail)
    routing = getattr(run, "routing", None) or {}
    if isinstance(routing, dict):
        chosen = routing.get("chosen_model")
        if isinstance(chosen, str) and chosen.strip():
            names.add(chosen.strip())
            tail = chosen.strip().rsplit("/", 1)[-1]
            if len(tail) >= 5:
                names.add(tail)
        for candidate in routing.get("candidates") or []:
            if isinstance(candidate, str) and candidate.strip():
                names.add(candidate.strip())
    return sorted(names, key=len, reverse=True)


def scrub(text: str | None, run: Any) -> str:
    """`text` without the run's model or provider names and without a leading
    `[provider]` error tag."""
    out = str(text or "")
    for name in _identities(run):
        out = re.sub(re.escape(name), REDACTED, out, flags=re.IGNORECASE)
    return _PROVIDER_TAG_RE.sub("", out, count=1) if out.lstrip().startswith("[") else out


RUN_LIMIT_TEXT = "the run stopped at a run limit before finishing"
_LIMIT_RE = re.compile(
    r"max_iterations|iteration limit|cost_cap|cost cap|output_budget|budget_exceeded|run limit|"
    r"max_cost|token limit|too many iterations|identical failing calls",
    re.IGNORECASE,
)
_NUMBERS_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?"  # dollar amounts
    r"|\b\d[\d,]*\s*(?:input |output |total )?tokens?\b"  # token counts
    r"|\b\d+\s*(?:iterations?|turns?)\b"  # iteration counts
    r"|\b(?:iterations?|turn)\s*#?\d+\b",
    re.IGNORECASE,
)


def sanitize(text: str) -> str:
    """`text` without dollar amounts, token counts or iteration counts."""
    return _NUMBERS_RE.sub("[n]", str(text or ""))


def clean_error(text: str | None, run: Any) -> str:
    """A run error as the judge may read it: a run-limit stop (iterations, spend,
    output budget) becomes neutral text, amounts and counts are removed and the
    model/provider names scrubbed."""
    raw = str(text or "")
    if _LIMIT_RE.search(raw):
        return RUN_LIMIT_TEXT
    return sanitize(scrub(raw, run))


def clip(text: Any, limit: int) -> str:
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= limit else s[: limit - 3].rstrip() + "..."


def _key_argument(arguments: Any) -> str:
    if not isinstance(arguments, dict) or not arguments:
        return ""
    for key in _KEY_ARGUMENTS:
        if arguments.get(key):
            return str(arguments[key])
    first = next(iter(arguments.values()))
    return first if isinstance(first, str) else json.dumps(first, default=str)


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") if isinstance(b, dict) else (b if isinstance(b, str) else "") for b in content
        )
    return ""


def run_digest(run: Any) -> str:
    """The run's tool calls in order (name and key argument), its tool errors,
    and its last assistant message; at most 60 lines, each cut to 160 characters,
    the message to 2,000. Scrubbed of model identity."""
    messages = list(getattr(run, "messages", None) or [])
    names: dict[str, str] = {}
    lines: list[str] = []
    final = ""
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "assistant":
            text = _message_text(m.get("content")).strip()
            if text:
                final = text
            for call in m.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                names[str(call.get("id"))] = str(call.get("name"))
                arg = clip(_key_argument(call.get("arguments")), DIGEST_LINE_CHARS)
                lines.append(f"- {call.get('name')}({arg})" if arg else f"- {call.get('name')}()")
        elif role == "tool":
            text = _message_text(m.get("content")).strip()
            if text.startswith("Tool error"):
                who = names.get(str(m.get("tool_call_id")), "tool")
                lines.append(f"  ! {who}: {clean_error(clip(text, DIGEST_LINE_CHARS), run)}")
    shown = lines[:DIGEST_MAX_LINES]
    body = "\n".join(scrub(line, run) for line in shown) or "(no tool calls)"
    if len(lines) > len(shown):
        body += f"\n... {len(lines) - len(shown)} more line(s) not shown"
    last = scrub(clip(final, FINAL_MESSAGE_CHARS), run) if final else "(none)"
    return f"{body}\n\nFinal message of the run:\n{last}"


def render_outcome(outcome: dict | None) -> str:
    """A reported outcome as `key: value` lines, empty fields left out."""
    if not isinstance(outcome, dict) or not outcome:
        return "(the run reported no outcome)"
    lines = []
    for key, value in outcome.items():
        if key == "reported_at" or value in (None, "", [], {}):
            continue
        shown = ", ".join(str(v) for v in value) if isinstance(value, list) else value
        lines.append(f"- {key}: {clip(shown, 600)}")
    return "\n".join(lines) or "(the run reported no outcome)"


def attached_documents(entries: list[tuple[Any, str]]) -> str:
    """The attached-documents section: an id and a purpose per document."""
    if not entries:
        # Said outright: a judge left to discover this calls search_documents,
        # gets a tool error and, seen live on 2026-11-06, answers in prose and
        # never submits a score.
        return (
            "None. No documents are attached to this run, so read_document and "
            "search_documents will fail: score from this pack alone and call submit_evaluation."
        )
    listed = "\n".join(f"- {doc.id}: {doc.filename} ({purpose})" for doc, purpose in entries)
    # A turn budget in the task itself: the harness doctrine says the same, but a
    # seeded harness keeps the doctrine it was created with, and a judge left to
    # verify every page-map boundary of a 300-page annual report used all its turns
    # and never scored (cloud, 2026-11-09).
    return (
        f"{listed}\n\nOpen these only to check a specific doubt, with at most three reads or searches "
        "in all, then call submit_evaluation. A run that ends without submit_evaluation records no score."
    )


def assemble(sections: list[tuple[str, str]], *, limit: int = PACK_MAX_CHARS) -> str:
    """Markdown from (heading, body) sections in priority order, cut at the cap:
    a section that does not fit whole is shortened, later ones are dropped."""
    out: list[str] = []
    used = 0
    for heading, body in sections:
        block = f"## {heading}\n{body.strip()}\n"
        if used + len(block) + 1 > limit:
            room = limit - used - 40
            if room > 200:
                out.append(block[:room].rstrip() + "\n[pack cut at the size limit]\n")
            break
        out.append(block)
        used += len(block) + 1
    return "\n".join(out)
