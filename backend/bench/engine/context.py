"""Context assembly: platform preamble → pack doctrine → task instructions →
harness extra. The doctrine block is a stable prefix across runs of a pack
version, which is what makes Anthropic prompt caching effective.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bench.db.models import Document, Harness, Pack, Run

PLATFORM_PREAMBLE = """\
You are an analyst working inside bench, a harness platform for rigorous, \
auditable knowledge work. Non-negotiable rules:

1. NEVER state or cite a numeric value you did not retrieve via the \
lookup_dataset tool in this run. Quote retrieved values verbatim in any \
cited_values field. Prose may describe magnitudes approximately, but the \
citation record must be exact.
2. Your structured outputs are DRAFTS. A named human reviewer approves or \
rejects them. Never claim an output is approved, blessed, or final.
3. Follow the doctrine documents below and cite them by their headings when \
your reasoning relies on them.
4. Be honest about uncertainty. If required data is missing, use \
file_data_request, then complete the task with what exists and reflect the \
gap in your confidence rating. An insufficient_data verdict is a valid, \
respectable outcome.
5. Write plain language for a non-technical reader: no tool names, no run \
ids, no jargon in methodology notes."""


def doctrine_sha(pack_dir: Path, doctrine_files: list[str]) -> str:
    h = hashlib.sha256()
    for rel in doctrine_files:
        h.update((pack_dir / rel).read_bytes())
    return h.hexdigest()


def _task_config(pack: Pack | None, task_type: str) -> dict | None:
    if pack is None:
        return None
    for t in pack.manifest.get("task_types", []):
        if t["slug"] == task_type:
            return t
    return None


CHAT_PREAMBLE = """\
## Current task: conversation

You are the analyst's conversational assistant. Answer questions directly \
using your document and dataset tools. When the analyst asks for work that a \
specialist task type in the capability catalog covers (an assessment, an \
extraction, a section draft, a QA review), delegate it with run_harness_task \
rather than attempting the structured work yourself — the specialist run \
carries its own doctrine, validation, and audit trail. Report delegated \
results faithfully, always noting that recorded findings are drafts awaiting \
human approval. If no specialist task fits and the request needs judgment you \
cannot ground in retrieved data, say so honestly."""


def assemble_system_prompt(
    harness: Harness,
    pack: Pack | None,
    task_type: str,
    output_schemas: dict[str, dict],
    extra_context: str | None = None,
) -> str:
    parts = [PLATFORM_PREAMBLE]

    if pack is not None:
        pack_dir = Path(pack.source_path)
        for rel in pack.manifest.get("doctrine", []):
            path = pack_dir / rel
            try:
                text = path.read_text()
            except OSError:
                continue
            file_sha = hashlib.sha256(text.encode()).hexdigest()[:12]
            parts.append(f'<doctrine file="{rel}" sha256="{file_sha}">\n{text}\n</doctrine>')

    task = _task_config(pack, task_type)
    if task:
        parts.append(f"## Current task: {task.get('display_name', task_type)}\n\n{task.get('instructions', '')}")
        schema_ref = task.get("output_schema_slug")
        if schema_ref and schema_ref in output_schemas:
            parts.append(
                "## Output contract\n\nYour terminal action is a call to "
                f"`{task.get('terminal_tool', 'record_verdict')}` with schema_slug "
                f"`\"{schema_ref}\"` and a payload matching this JSON Schema exactly:\n\n"
                f"```json\n{json.dumps(output_schemas[schema_ref], indent=2)}\n```"
            )
    elif task_type == "chat":
        parts.append(CHAT_PREAMBLE)
    elif task_type == "freeform":
        parts.append(
            "## Current task: freeform\n\nAssist the analyst with their request, "
            "using the available tools and honoring all platform rules."
        )

    if extra_context:
        parts.append(extra_context)

    if harness.system_prompt_extra:
        parts.append(f"## Additional harness instructions\n\n{harness.system_prompt_extra}")

    return "\n\n".join(parts)


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
            size_note = f"{len(d.extracted_text or '')} chars extracted" if d.extracted_text else d.extraction_status
            lines.append(f"- {d.id} — {d.filename} ({size_note})")
    return "\n".join(lines)
