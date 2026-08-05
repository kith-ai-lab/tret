"""Context assembly: platform preamble → pack doctrine → task instructions →
output contract → harness extra. The doctrine block is a stable prefix across
runs of a pack version, which is what makes Anthropic prompt caching effective.

Assembly is *accounted*. `assemble_context` returns the prompt plus a
per-component breakdown (`ContextBlock`s) whose estimated tokens are persisted
on the run, so token spend is legible per component instead of arriving as one
opaque `input_tokens` number. Estimation is chars/4: no tokenizer dependency,
provider-independent, and accurate enough to see what a component costs.

Doctrine can be task-scoped. A pack task type may declare
`doctrine: ["02-procedure.md", "03-reason-codes.md#outdated_inputs"]` to load
only the files (or `#`/`##` sections) it actually needs. Declaring nothing loads
every doctrine file in the pack — the historical behavior, so existing packs are
unaffected. Whatever is loaded is hashed and recorded per block, so the audit
trail states exactly what the model saw.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from bench.db.models import Document, Harness, Pack, Run

# chars/4 — deliberately dependency-free. Real usage is recorded per run from
# provider-reported tokens; this is for composition accounting and routing size
# hints, where a stable cheap estimate beats an exact expensive one.
TOKEN_ESTIMATOR = "chars/4"


def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


PLATFORM_PREAMBLE = """\
You are an analyst working inside bench, a harness platform for rigorous, \
auditable knowledge work. Non-negotiable rules:

1. NEVER state or cite a numeric value you did not retrieve via the \
lookup_dataset tool in this run. Quote retrieved values verbatim in any \
cited_values field. Prose may describe magnitudes loosely; the citation record \
must be exact.
2. Your structured outputs are DRAFTS awaiting a named human reviewer. Never \
claim an output is approved or final.
3. Follow the doctrine below; cite it by heading wherever your reasoning relies \
on it.
4. Be honest about uncertainty. If required data is missing, call \
file_data_request, then finish with what exists and reflect the gap in your \
confidence rating. An insufficient_data verdict is a valid, respectable outcome.
5. Write plain language for a non-technical reader: no tool names, no ids, no \
jargon in notes."""


CHAT_PREAMBLE = """\
## Current task: conversation

You are the analyst's conversational assistant. Answer questions directly with \
your document and dataset tools. When the analyst asks for work a specialist \
task type in the capability catalog covers (an assessment, an extraction, a \
section draft, a QA review), delegate it with run_harness_task instead of doing \
the structured work yourself — the specialist run carries its own doctrine, \
validation, and audit trail. Report delegated results faithfully, always noting \
that recorded findings are drafts awaiting human approval. If no specialist task \
fits and the request needs judgment you cannot ground in retrieved data, say so \
honestly."""

FREEFORM_PREAMBLE = """\
## Current task: freeform

Assist the analyst with their request, using the available tools and honoring \
all platform rules."""


# ── composition accounting ────────────────────────────────────────────────────
@dataclass
class ContextBlock:
    """One accounted component of the assembled context."""

    kind: str  # platform_preamble|doctrine|task_instructions|output_schema|...
    label: str
    chars: int
    est_tokens: int
    sha256: str | None = None  # set for doctrine: hash of the text actually loaded
    sections: list[str] | None = None  # doctrine sections loaded (None = whole file)
    parts: dict[str, int] | None = None  # sub-breakdown, e.g. est tokens per tool
    note: str | None = None
    text: str = ""  # the rendered prompt text; never persisted

    def to_json(self) -> dict:
        out = {
            "kind": self.kind,
            "label": self.label,
            "chars": self.chars,
            "est_tokens": self.est_tokens,
        }
        for name in ("sha256", "sections", "parts", "note"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out


def block_for(kind: str, label: str, text: str, **extra) -> ContextBlock:
    return ContextBlock(
        kind=kind,
        label=label,
        chars=len(text),
        est_tokens=estimate_tokens(text),
        text=text,
        **extra,
    )


def tool_spec_block(tool_specs: list) -> ContextBlock:
    """Account for the tool definitions sent alongside the system prompt."""
    parts: dict[str, int] = {}
    total_chars = 0
    for spec in tool_specs:
        wire = json.dumps(
            {"name": spec.name, "description": spec.description, "input_schema": spec.parameters},
            separators=(",", ":"),
        )
        parts[spec.name] = estimate_tokens(wire)
        total_chars += len(wire)
    return ContextBlock(
        kind="tool_specs",
        label=f"{len(tool_specs)} tools",
        chars=total_chars,
        est_tokens=sum(parts.values()),
        parts=parts or None,
    )


def composition_report(blocks: list[ContextBlock]) -> dict:
    """The persisted, API-exposed breakdown of what a run's context is made of."""
    by_kind: dict[str, int] = {}
    for b in blocks:
        by_kind[b.kind] = by_kind.get(b.kind, 0) + b.est_tokens
    return {
        "estimator": TOKEN_ESTIMATOR,
        "total_est_tokens": sum(b.est_tokens for b in blocks),
        "total_chars": sum(b.chars for b in blocks),
        "by_kind": by_kind,
        "blocks": [b.to_json() for b in blocks],
    }


@dataclass
class AssembledContext:
    system: str
    blocks: list[ContextBlock] = field(default_factory=list)


# ── doctrine selection ────────────────────────────────────────────────────────
def doctrine_sha(pack_dir: Path, doctrine_files: list[str]) -> str:
    """Pack-level hash over every doctrine file — the pack's doctrine identity.

    Unaffected by task scoping: what a given run actually loaded is recorded
    per block in the run's context composition.
    """
    h = hashlib.sha256()
    for rel in doctrine_files:
        h.update((pack_dir / rel).read_bytes())
    return h.hexdigest()


_HEADING = re.compile(r"^(#{1,6})[ \t]+(\S.*?)[ \t]*$")


def parse_doctrine_selector(selector: str) -> tuple[str, str | None]:
    """`"file.md#Some Heading"` → `("file.md", "Some Heading")`."""
    path, _, section = selector.partition("#")
    section = section.strip()
    return path.strip(), section or None


def task_doctrine_selection(
    pack_doctrine: list[str], declared: list[str] | None
) -> list[tuple[str, list[str]]]:
    """Resolve a task's doctrine declaration to `[(file, [sections])]`.

    No declaration → every pack doctrine file, whole (today's behavior). Only
    files listed in the pack's own `doctrine:` list are ever loaded, so a task
    declaration cannot reach other files in the pack directory.
    """
    if not declared:
        return [(rel, []) for rel in pack_doctrine]
    known = set(pack_doctrine)
    selection: list[tuple[str, list[str]]] = []
    index: dict[str, list[str]] = {}
    for selector in declared:
        rel, section = parse_doctrine_selector(selector)
        if rel not in known:
            continue
        if rel not in index:
            index[rel] = []
            selection.append((rel, index[rel]))
        # "*" is an internal marker for "whole file", which wins over any
        # section of the same file however the two are ordered.
        index[rel].append(section or "*")
    return [(rel, [] if "*" in sections else sections) for rel, sections in selection]


def _headings(lines: list[str]) -> list[tuple[int, str, int]]:
    out = []
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if m:
            out.append((len(m.group(1)), m.group(2), i))
    return out


def _match_heading(headings: list[tuple[int, str, int]], wanted: str) -> int | None:
    """Index into `headings` for `wanted`: exact, else prefix, else substring."""
    target = wanted.casefold().strip()
    titles = [h[1].casefold().strip() for h in headings]
    for tier in (
        lambda t: t == target,
        lambda t: t.startswith(target),
        lambda t: target in t,
    ):
        for i, title in enumerate(titles):
            if tier(title):
                return i
    return None


def select_doctrine_text(text: str, sections: list[str]) -> tuple[str, list[str], list[str]]:
    """Extract `sections` from a doctrine file.

    Returns `(text, matched_headings, unresolved_selectors)`. The file's front
    matter (title + framing paragraphs before the first `##`) always rides along
    — it is cheap and usually carries the rule that makes the sections
    interpretable. Anything unresolved fails OPEN: the whole file is returned, so
    a bad selector can never silently starve a task of doctrine.
    """
    if not sections:
        return text, [], []
    lines = text.splitlines()
    headings = _headings(lines)
    if not headings:
        return text, [], list(sections)

    spans: list[tuple[int, int]] = []
    matched: list[str] = []
    unresolved: list[str] = []
    for wanted in sections:
        i = _match_heading(headings, wanted)
        if i is None:
            unresolved.append(wanted)
            continue
        level, title, start = headings[i]
        if level == 1:
            return text, [title], []  # a top-level selector is the whole document
        end = len(lines)
        for next_level, _, next_start in headings[i + 1 :]:
            if next_level <= level:
                end = next_start
                break
        spans.append((start, end))
        matched.append(title)
    if unresolved or not spans:
        return text, matched, unresolved

    merged: list[tuple[int, int]] = []
    for start, end in sorted(set(spans)):
        if merged and start <= merged[-1][1]:  # nested or adjacent: one chunk
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            continue
        merged.append((start, end))

    front_end = next((start for level, _, start in headings if level >= 2), 0)
    chunks = ["\n".join(lines[:front_end]).strip()] if front_end else []
    for start, end in merged:
        chunks.append("\n".join(lines[start:end]).strip())
    return "\n\n".join(c for c in chunks if c), matched, []


def doctrine_blocks(pack: Pack, task: dict | None) -> list[ContextBlock]:
    """Doctrine blocks for a run: the loaded text, hashed, with its own prompt tags."""
    pack_dir = Path(pack.source_path)
    blocks: list[ContextBlock] = []
    selection = task_doctrine_selection(
        list(pack.manifest.get("doctrine", [])), (task or {}).get("doctrine")
    )
    for rel, sections in selection:
        try:
            text = (pack_dir / rel).read_text()
        except OSError:
            continue
        loaded, matched, unresolved = select_doctrine_text(text, sections)
        file_sha = hashlib.sha256(loaded.encode()).hexdigest()
        attrs = f'file="{rel}"'
        if matched and not unresolved:
            attrs += f' sections="{"; ".join(matched)}"'
        rendered = f'<doctrine {attrs} sha256="{file_sha[:12]}">\n{loaded}\n</doctrine>'
        blocks.append(
            block_for(
                "doctrine",
                rel,
                rendered,
                sha256=file_sha,
                sections=matched if (matched and not unresolved) else None,
                note=(
                    f"declared sections not found ({', '.join(unresolved)}); full file loaded"
                    if unresolved
                    else None
                ),
            )
        )
    return blocks


def _task_config(pack: Pack | None, task_type: str) -> dict | None:
    if pack is None:
        return None
    for t in pack.manifest.get("task_types", []):
        if t["slug"] == task_type:
            return t
    return None


# ── assembly ──────────────────────────────────────────────────────────────────
def assemble_context(
    harness: Harness,
    pack: Pack | None,
    task_type: str,
    output_schemas: dict[str, dict],
    extra_context: str | None = None,
) -> AssembledContext:
    """Build the system prompt and its per-component token accounting."""
    blocks: list[ContextBlock] = [
        block_for("platform_preamble", "platform_preamble", PLATFORM_PREAMBLE)
    ]

    task = _task_config(pack, task_type)
    if pack is not None:
        blocks.extend(doctrine_blocks(pack, task))

    if task:
        instructions = (
            f"## Current task: {task.get('display_name', task_type)}\n\n"
            f"{task.get('instructions', '')}"
        )
        blocks.append(block_for("task_instructions", task_type, instructions))
        schema_ref = task.get("output_schema_slug")
        if schema_ref and schema_ref in output_schemas:
            contract = (
                "## Output contract\n\nYour terminal action is a call to "
                f"`{task.get('terminal_tool', 'record_verdict')}` with schema_slug "
                f'`"{schema_ref}"` and a payload matching this JSON Schema exactly:\n\n'
                f"```json\n{json.dumps(output_schemas[schema_ref], separators=(',', ':'))}\n```"
            )
            blocks.append(block_for("output_schema", schema_ref, contract))
    elif task_type == "chat":
        blocks.append(block_for("task_instructions", "chat", CHAT_PREAMBLE))
    elif task_type == "freeform":
        blocks.append(block_for("task_instructions", "freeform", FREEFORM_PREAMBLE))

    if extra_context:
        blocks.append(block_for("extra_context", "capability_catalog", extra_context))

    if harness.system_prompt_extra:
        extra = f"## Additional harness instructions\n\n{harness.system_prompt_extra}"
        blocks.append(block_for("harness_extra", harness.name, extra))

    return AssembledContext(system="\n\n".join(b.text for b in blocks), blocks=blocks)


def assemble_system_prompt(
    harness: Harness,
    pack: Pack | None,
    task_type: str,
    output_schemas: dict[str, dict],
    extra_context: str | None = None,
) -> str:
    """The system prompt alone (harness preview endpoint, and back-compat)."""
    return assemble_context(
        harness, pack, task_type, output_schemas, extra_context=extra_context
    ).system


def build_user_message(run: Run, pack: Pack | None, documents: list[Document]) -> str:
    task = _task_config(pack, run.task_type)
    lines: list[str] = []
    if run.task_type == "chat":
        lines.append(str(run.task_input.get("message", "")))
    elif task and run.task_type != "freeform":
        lines.append(f"Task: {task.get('display_name', run.task_type)}")
        lines.append(f"Parameters: {json.dumps(run.task_input)}")
    else:
        lines.append(str(run.task_input.get("message", "")))

    if documents:
        lines.append("\nAttached documents (read them with read_document / search_documents):")
        for d in documents:
            size_note = (
                f"{len(d.extracted_text or '')} chars extracted"
                if d.extracted_text
                else d.extraction_status
            )
            lines.append(f"- {d.id} — {d.filename} ({size_note})")
    return "\n".join(lines)
