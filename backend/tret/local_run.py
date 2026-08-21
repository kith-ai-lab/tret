"""The headless CLI's runner: a single-shot agentic loop over local files.

    $ tret run "summarize the 40 filings in ./q3" --path ./q3
    → routed to qwen3.8 · extraction, long-context, no frontier needed
    ✓ summary written to q3-filings.md
    receipt · $0.021 · 0.4 gCO₂e · ledger #b7a2

`arun()` routes the task via the existing `ModelRouter` (the same wiring
`tret.sdk.Router` uses — `get_catalog`, `ProviderRegistry`, `ModelRouter`,
`NoPriors`), then loops the chosen model with three read-only local-file
tools, mirroring the message protocol `engine/harness.py`'s loop uses turn by
turn (cited inline below, rather than imported: `engine/*` pulls in
SQLAlchemy and the rest of the server stack, and this module has to stay off
that path — see `tests/test_sdk_import_hygiene.py`). Once the loop ends it
builds a `Receipt` via `tret.sdk._build_receipt` on the run's summed usage and
appends one line to the local ledger.

Core-install only: no DB, no FastAPI, no network beyond whatever the routed
model's provider needs.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tret.config import get_settings
from tret.providers.base import (
    Msg,
    Provider,
    ProviderError,
    TextDelta,
    ToolCall,
    ToolCallComplete,
    ToolSpec,
    TurnComplete,
    Usage,
)
from tret.providers.catalog import ModelCatalog, ModelInfo, ProviderRegistry, get_catalog
from tret.router_llm.objectives import DEFAULT_MAX_COST_TIER, DEFAULT_OBJECTIVE, OBJECTIVES
from tret.router_llm.priors_base import NoPriors
from tret.router_llm.router import (
    TIER_ORDER,
    ModelRouter,
    RoutingDecision,
    RoutingUnavailable,
)
from tret.sdk import Receipt, _build_receipt, _usage_is_empty

__all__ = [
    "LocalRunResult",
    "ToolError",
    "DEFAULT_MAX_ITERATIONS",
    "DEFAULT_LEDGER_PATH",
    "arun",
]

# How much of the task the router's own prompt is shown — mirrors
# tret.sdk._TASK_DESCRIPTION_CHARS exactly, same rationale: the router only
# needs enough of the task to classify it, and its own prompt is itself a cost.
_TASK_DESCRIPTION_CHARS = 200

DEFAULT_MAX_ITERATIONS = 24

_BASE_SYSTEM = (
    "You are a careful, direct assistant running as a headless CLI. Read the "
    "request and respond to it precisely and concisely, in plain text."
)
_TOOLS_SYSTEM_SUFFIX = (
    " You have read-only tools to look at local files: list_files, read_file and "
    "search_files. Use them as needed to ground your answer, then give your final "
    "answer as plain text with no further tool calls."
)


def _system_prompt(*, has_tools: bool) -> str:
    return _BASE_SYSTEM + (_TOOLS_SYSTEM_SUFFIX if has_tools else "")


class ToolError(Exception):
    """Returned to the model as a tool error message; never fatal to the run.

    Local counterpart of `engine/tools.py`'s `ToolError` — same role, kept as a
    separate class because importing the engine's would drag in SQLAlchemy.
    """


# ── path safety ─────────────────────────────────────────────────────────────
# Non-negotiable: every path a tool touches resolves under `root`, or the
# model gets a tool-error message, never a crash. `.resolve()` also collapses
# symlinks, so a symlink planted inside root that points outside it is caught
# by the same `.is_relative_to()` check that catches `../..` traversal and an
# absolute path — `(root / "/etc/passwd")` is `Path("/etc/passwd")` in
# pathlib's own join semantics, so it is rejected here too, not silently
# reinterpreted as relative.
MAX_READ_BYTES = 10 * 1024 * 1024  # pre-extraction cap on one file
READ_CHUNK_CHARS = 20_000
MAX_LIST_ENTRIES = 500

# Extensions list_files/search_files skip outright: formats read_file cannot
# usefully turn into text (images, audio/video, archives, executables, fonts,
# compiled objects). Deliberately does NOT include .pdf/.docx — those ARE
# readable, via the dispatch in `_extract_text`.
_BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp", ".tiff",
    ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".wav", ".flac", ".ogg", ".m4a",
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".class", ".jar", ".pyc", ".o", ".a", ".wasm",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".db", ".sqlite", ".sqlite3",
}


def _resolve_safe(root: Path, path: str) -> Path:
    """Resolve `path` under `root`. Raises `ToolError`, never anything else."""
    root_resolved = root.resolve()
    try:
        resolved = (root / path).resolve()
    except OSError as e:
        raise ToolError(f"could not resolve path {path!r}: {e}") from e
    if not resolved.is_relative_to(root_resolved):
        raise ToolError(f"path {path!r} is outside the run's root directory")
    return resolved


def _iter_readable_files(root: Path):
    """Every non-hidden, non-obviously-binary, in-bounds file under `root`,
    depth-first.

    `followlinks=False` (os.walk's default) stops recursion into a symlinked
    *directory*, so a symlink loop cannot make this hang — but it does nothing
    about a symlink to a *file*: os.walk never descends into a file, so
    `followlinks` never applies to it, and it lands in `filenames` right next
    to real files. A plain `Path(dirpath) / name` join stays textually under
    `root` even when the target it resolves to is not, so every candidate is
    `.resolve()`d and checked against `root` here, before it is ever yielded —
    the same confinement `_resolve_safe` enforces for `read_file`, applied up
    front so `list_files`/`search_files` never even list (let alone read) a
    path outside the run's root directory.
    """
    root_resolved = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root_resolved, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            if p.suffix.lower() in _BINARY_EXTENSIONS:
                continue
            try:
                resolved = p.resolve()
            except OSError:
                continue  # unresolvable (e.g. a dangling symlink): skip, not fatal
            if not resolved.is_relative_to(root_resolved):
                continue  # symlink to a file outside root: refused, same as read_file
            yield p


# ── extraction (dispatch is unit-tested with monkeypatched extractors) ──────
def _extract_pdf_text(path: Path) -> str:
    import pypdf

    reader = pypdf.PdfReader(str(path))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def _extract_docx_text(path: Path) -> str:
    import docx

    document = docx.Document(str(path))
    return "\n".join(p.text for p in document.paragraphs)


def _extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_text(path)
    if suffix == ".docx":
        return _extract_docx_text(path)
    return path.read_text(encoding="utf-8", errors="replace")


# ── file tools ────────────────────────────────────────────────────────────────
@dataclass
class _ToolCtx:
    root: Path


async def _handle_list_files(ctx: _ToolCtx) -> str:
    root_resolved = ctx.root.resolve()
    paths = sorted(str(p.relative_to(root_resolved)) for p in _iter_readable_files(ctx.root))
    if not paths:
        return "(no readable files found under the run's root directory)"
    total = len(paths)
    shown = paths[:MAX_LIST_ENTRIES]
    note = ""
    if total > MAX_LIST_ENTRIES:
        note = (
            f"\n\n[TRUNCATED: showing {len(shown)} of {total} files "
            f"(cap: {MAX_LIST_ENTRIES}). Narrow with search_files or a subdirectory.]"
        )
    return "\n".join(shown) + note


async def _handle_read_file(ctx: _ToolCtx, path: str, offset: int = 0) -> str:
    resolved = _resolve_safe(ctx.root, path)
    root_resolved = ctx.root.resolve()
    # Enforce the same policy `_iter_readable_files` applies to list_files and
    # search_files (and that read_file's own tool description advertises):
    # a dotfile, or any path under a dot-directory, is refused outright, as is
    # a binary extension — a model that already knows a hidden/binary path
    # exists (e.g. from a prior run, or a guess) cannot use read_file to get
    # at it just because it skipped the listing.
    if any(part.startswith(".") for part in resolved.relative_to(root_resolved).parts):
        raise ToolError(f"'{path}' is hidden (a dotfile or under a dot-directory); refused")
    if resolved.suffix.lower() in _BINARY_EXTENSIONS:
        raise ToolError(f"'{path}' has a binary extension; refused")
    if not resolved.is_file():
        raise ToolError(f"'{path}' is not a file")
    try:
        size = resolved.stat().st_size
    except OSError as e:
        raise ToolError(f"could not stat {path!r}: {e}") from e
    if size > MAX_READ_BYTES:
        raise ToolError(
            f"'{path}' is {size} bytes, over the {MAX_READ_BYTES}-byte pre-extraction cap"
        )
    try:
        text = _extract_text(resolved)
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"could not read {path!r}: {type(e).__name__}: {e}") from e
    offset = max(0, offset)
    chunk = text[offset : offset + READ_CHUNK_CHARS]
    remaining = max(0, len(text) - offset - READ_CHUNK_CHARS)
    # Mirrors engine/tools.py::read_document's truncation marker (tools.py
    # ~176-179): same "N more characters; call again with offset=" wording, so
    # a model that has ever paged through an attached document recognizes it.
    suffix = (
        f"\n\n[... {remaining} more characters; call again with offset={offset + READ_CHUNK_CHARS}]"
        if remaining
        else ""
    )
    rel = resolved.relative_to(root_resolved)
    return f"# {rel} (chars {offset}-{offset + len(chunk)} of {len(text)})\n\n{chunk}{suffix}"


async def _handle_search_files(ctx: _ToolCtx, query: str, max_results: int = 8) -> str:
    if not query.strip():
        raise ToolError("query must not be empty")
    # The tool spec's JSON Schema "maximum": 50 is advisory only — nothing
    # stops a model (or a hand-crafted tool call) from sending 1000 anyway, so
    # it is clamped here, server-side, same floor of 1 the schema also implies.
    max_results = max(1, min(max_results, 50))
    q = query.lower()
    root_resolved = ctx.root.resolve()
    hits: list[str] = []
    for p in _iter_readable_files(ctx.root):
        if len(hits) >= max_results:
            break
        try:
            if p.stat().st_size > MAX_READ_BYTES:
                continue
            text = _extract_text(p)
        except Exception:
            continue  # unreadable file: skip it, not a run-ending error
        rel = p.relative_to(root_resolved)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if q in line.lower():
                snippet = line.strip()
                if len(snippet) > 200:
                    snippet = snippet[:200] + "…"
                hits.append(f"{rel}:{lineno}: {snippet}")
                if len(hits) >= max_results:
                    break
    if not hits:
        return f"No matches for {query!r}."
    return "\n".join(hits)


def _tool_specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="list_files",
            description=(
                "List files under the run's root directory (recursive; hidden files "
                "and obviously-binary files are skipped)."
            ),
            parameters={"type": "object", "properties": {}},
            handler=_handle_list_files,
        ),
        ToolSpec(
            name="read_file",
            description=(
                "Read the extracted text of a file under the run's root directory. "
                "Use offset to page through a long file."
            ),
            parameters={
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the root directory"},
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": "Character offset",
                    },
                },
            },
            handler=_handle_read_file,
        ),
        ToolSpec(
            name="search_files",
            description="Case-insensitive substring search over readable files under the root.",
            parameters={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8},
                },
            },
            handler=_handle_search_files,
        ),
    ]


async def _unknown_tool(name: str) -> tuple[str, bool]:
    # Same message and shape as engine/harness.py::_unknown_tool — duplicated
    # rather than imported for the same reason ToolError is: harness.py is not
    # on the core import path.
    return f"Tool error: '{name}' is not an enabled tool for this run.", True


async def _execute_tool(ctx: _ToolCtx, spec: ToolSpec, arguments: dict) -> tuple[str, bool]:
    """Run one tool call. Returns (result_text, is_error).

    Mirrors engine/tools.py::execute_tool's error handling: a ToolError, a bad
    argument, or a bug in the handler all become a tool-result message, never
    an exception that would escape the run loop (tools.py ~997-1006).
    """
    try:
        result = await spec.handler(ctx, **arguments)
        return result, False
    except ToolError as e:
        return f"Tool error: {e}", True
    except TypeError as e:
        return f"Tool error: invalid arguments — {e}", True
    except Exception as e:  # tool bugs shouldn't kill the run
        return f"Tool error: unexpected failure — {type(e).__name__}: {e}", True


# ── the loop ──────────────────────────────────────────────────────────────────
async def _run_agentic_loop(
    provider: Provider,
    model: ModelInfo,
    *,
    system: str,
    task: str,
    tool_specs: list[ToolSpec],
    tool_ctx: _ToolCtx | None,
    max_tokens: int,
    temperature: float,
    max_iterations: int,
    on_tool_call: Callable[[ToolCall], None] | None = None,
) -> tuple[str, str, Usage, str, int, bool, str | None]:
    """route → stream → execute tool calls → append messages → repeat.

    The message protocol below is copied from engine/harness.py's loop, not
    reinvented:

    * The assistant turn's Msg is appended unconditionally — accumulated text
      (or None) plus the turn's tool_calls together, in one message — before
      the loop decides whether the turn ended the run (harness.py ~604-611).
    * Each tool call gets exactly one `role="tool"` reply Msg, keyed to that
      call's `tool_call_id`, carrying `meta={"error": is_error}`, appended in
      call order (harness.py ~757-761).
    * A turn with no tool calls ends the run (harness.py ~641-643, the
      `if not tool_calls:` branch — minus the terminal-tool nudge, which is a
      pack-task concept this freeform loop has no equivalent of).

    Returns (text, stop_reason, total_usage, status, iterations_used,
    usage_reported, error).
    """
    messages: list[Msg] = [Msg(role="user", content=task)]
    total_usage = Usage()
    text_so_far = ""
    stop_reason = ""
    status = "completed"
    iteration = 0
    error: str | None = None
    # Usage policy (matches tret.sdk's single-call semantics, extended to many
    # turns): the receipt may only be priced when EVERY completed turn ended in
    # a TurnComplete carrying non-empty usage. One silent turn out of four
    # would otherwise yield a confident receipt for a quarter of the real
    # tokens — the exact claim Receipt's docstring forbids.
    turns_completed = 0
    all_turns_reported = True

    for iteration in range(1, max_iterations + 1):
        assistant_text: list[str] = []
        tool_calls: list[ToolCall] = []
        turn: TurnComplete | None = None
        try:
            async for event in provider.stream(
                model=model.wire_id,
                system=system,
                messages=messages,
                tools=tool_specs,
                max_tokens=max_tokens,
                temperature=temperature,
            ):
                if isinstance(event, TextDelta):
                    assistant_text.append(event.text)
                elif isinstance(event, ToolCallComplete):
                    # When no tools were offered, a stray call is ignored the
                    # way tret.sdk ignores it — replying "unknown tool" would
                    # invite a misbehaving model to burn every remaining
                    # iteration on a run that should have been one turn.
                    if tool_specs:
                        tool_calls.append(event.tool_call)
                elif isinstance(event, TurnComplete):
                    turn = event
        except ProviderError as e:
            # Tokens from earlier turns are already booked in `total_usage` and
            # already cost money — a failed run must still end in a receipt and
            # a ledger line, so record the failure and stop instead of letting
            # the exception unwind past the accounting (mirrors the spirit of
            # engine/harness.py's in-loop provider-error handling).
            status = "failed"
            error = str(e)
            partial = "".join(assistant_text)
            if partial:
                text_so_far = partial
            break

        usage = turn.usage if turn else Usage()
        turns_completed += 1
        if turn is None or _usage_is_empty(usage):
            all_turns_reported = False
        total_usage.input_tokens += usage.input_tokens
        total_usage.output_tokens += usage.output_tokens
        total_usage.cache_read_tokens += usage.cache_read_tokens
        total_usage.cache_write_tokens += usage.cache_write_tokens

        text_so_far = "".join(assistant_text)
        stop_reason = turn.stop_reason if turn else stop_reason

        messages.append(Msg(role="assistant", content=text_so_far or None, tool_calls=tool_calls))

        if not tool_calls:
            break

        for tc in tool_calls:
            if on_tool_call is not None:
                on_tool_call(tc)
            spec = next((s for s in tool_specs if s.name == tc.name), None)
            if spec is None:
                result_text, is_error = await _unknown_tool(tc.name)
            else:
                if tool_ctx is None:  # matched spec without a root: impossible by construction
                    raise RuntimeError("tool spec offered without a tool context")
                result_text, is_error = await _execute_tool(tool_ctx, spec, tc.arguments)
            messages.append(
                Msg(role="tool", content=result_text, tool_call_id=tc.id, meta={"error": is_error})
            )
    else:
        # The for/else fires only when the loop was never `break`-ed out of —
        # i.e. every turn up to and including max_iterations still had tool
        # calls. Whatever text that final turn produced (often none) is kept,
        # per the module contract: a truthful "ran out of budget", not a
        # fabricated answer.
        status = "hit_iteration_cap"

    usage_reported = turns_completed > 0 and all_turns_reported
    return text_so_far, stop_reason, total_usage, status, iteration, usage_reported, error


# ── routing wiring (same seam as tret.sdk: get_catalog / ProviderRegistry /
# ModelRouter / NoPriors, monkeypatched the same way in tests) ──────────────
def _wire() -> tuple[ModelCatalog, ProviderRegistry, ModelRouter]:
    catalog = get_catalog()
    registry = ProviderRegistry()
    router = ModelRouter(catalog, registry, NoPriors())
    return catalog, registry, router


# ── ledger ────────────────────────────────────────────────────────────────────
DEFAULT_LEDGER_PATH = Path.home() / ".tret" / "ledger.jsonl"


def _ledger_path() -> Path:
    configured = (get_settings().ledger_path or "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_LEDGER_PATH


def _ledger_entry(
    *, task: str, model: str, status: str, iterations: int, receipt: Receipt, out: str | None
) -> dict:
    overhead_usd = receipt.overhead.get("cost_usd") if receipt.overhead is not None else None
    return {
        "id": uuid.uuid4().hex,
        "ts": datetime.now(timezone.utc).isoformat(),
        "task": task[:200],
        "model": model,
        "status": status,
        "iterations": iterations,
        "usd": receipt.usd,
        "co2e_g": receipt.co2e_g,
        "energy_wh": receipt.energy_wh,
        "avoided_usd_pct": receipt.avoided_usd_pct,
        "avoided_co2e_pct": receipt.avoided_co2e_pct,
        "overhead_usd": overhead_usd,
        "out": out,
    }


def _append_ledger(entry: dict) -> None:
    path = _ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 0o600: ledger lines carry the first 200 chars of every task the user
        # ran — their words, not something other local users need to read.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError as e:
        # A ledger nobody can write to must never take the run down with it —
        # the receipt was already earned; print and move on.
        print(f"warning: could not append ledger entry to {path}: {e}", file=sys.stderr)


# ── the public entry point ──────────────────────────────────────────────────
@dataclass
class LocalRunResult:
    text: str
    model: str  # tret model id, e.g. "anthropic/claude-haiku-4-5"
    receipt: Receipt
    stop_reason: str
    status: str  # "completed" | "hit_iteration_cap" | "failed"
    iterations: int
    ledger_id: str
    error: str | None = None  # set when status == "failed" (the provider error)


async def arun(
    task: str,
    *,
    path: str | os.PathLike | None = None,
    out: str | None = None,
    objective: str = DEFAULT_OBJECTIVE,
    max_cost_tier: str = DEFAULT_MAX_COST_TIER,
    model: str | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    on_route: Callable[[RoutingDecision], None] | None = None,
    on_tool_call: Callable[[ToolCall], None] | None = None,
) -> LocalRunResult:
    """Route `task`, run it (with local-file tools when `path` is given),
    build a Receipt, append a ledger entry, and return all of it.

    `on_route` / `on_tool_call` are optional progress hooks — the CLI uses
    them to print to stderr; offline tests and library callers can ignore
    them. `path` omitted means no file tools are offered: the loop then
    behaves like `tret.sdk.Router.arun` (one turn, no tools), plus the ledger.
    """
    if max_cost_tier not in TIER_ORDER:
        valid = ", ".join(sorted(TIER_ORDER, key=TIER_ORDER.__getitem__))
        raise ValueError(f"max_cost_tier={max_cost_tier!r} is not valid; choose one of: {valid}")
    if objective not in OBJECTIVES:
        valid = ", ".join(OBJECTIVES)
        raise ValueError(f"objective={objective!r} is not valid; choose one of: {valid}")

    root: Path | None = None
    if path is not None:
        root = Path(path).resolve()
        if not root.is_dir():
            raise ValueError(f"--path {os.fspath(path)!r} is not a directory")

    catalog, registry, router = _wire()
    await catalog.warm_once()

    n_documents = sum(1 for _ in _iter_readable_files(root)) if root is not None else 0

    model_policy = {
        "mode": "pinned" if model else "auto",
        "model": model,
        "objective": objective,
        "allowed": None,
        "max_cost_tier": max_cost_tier,
    }
    decision = await router.route(
        model_policy=model_policy,
        task_type="freeform",
        task_shape="freeform",
        task_description=task[:_TASK_DESCRIPTION_CHARS],
        output_contract="free text",
        n_documents=n_documents,
        est_input_tokens=max(1, len(task) // 4),
    )
    if on_route is not None:
        on_route(decision)

    model_info = catalog.get(decision.chosen_model)
    if model_info is None:
        raise RoutingUnavailable(
            f"Routed to '{decision.chosen_model}', which is no longer in the catalog."
        )
    provider: Provider = registry.get(model_info.provider)

    tool_ctx = _ToolCtx(root=root) if root is not None else None
    tool_specs = _tool_specs() if root is not None else []

    text, stop_reason, usage, status, iterations, usage_reported, error = await _run_agentic_loop(
        provider,
        model_info,
        system=_system_prompt(has_tools=root is not None),
        task=task,
        tool_specs=tool_specs,
        tool_ctx=tool_ctx,
        max_tokens=max_tokens,
        temperature=temperature,
        max_iterations=max_iterations,
        on_tool_call=on_tool_call,
    )

    # Single model per run, so summing tokens across turns then pricing once
    # equals pricing per turn and summing — both cost_usd and the
    # weighted-token energy estimate are linear in tokens (see
    # tret.services.emissions and ModelInfo.cost_usd).
    receipt = _build_receipt(model_info, usage, decision, catalog, usage_reported)

    entry = _ledger_entry(
        task=task, model=model_info.id, status=status, iterations=iterations, receipt=receipt, out=out
    )
    _append_ledger(entry)

    return LocalRunResult(
        text=text,
        model=model_info.id,
        receipt=receipt,
        stop_reason=stop_reason,
        status=status,
        iterations=iterations,
        ledger_id=entry["id"],
        error=error,
    )
