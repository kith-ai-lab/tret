"""Offline tests for `tret.local_run`: the headless CLI's agentic loop.

No network, no DB. Follows tests/test_sdk.py's pattern exactly — a scripted
`Provider` stands in for the model, and `local_run.get_catalog` /
`local_run.ProviderRegistry` are monkeypatched to fakes, the same seam
`tret.sdk`'s own tests use (`tret.sdk` and `tret.local_run` share the same
lazy-wiring shape by design).

Every test that runs a full loop pins the model (`model=TARGET_MODEL.id`),
which bypasses the LLM router entirely (see router.py's `mode == "pinned"`
branch) — so the scripted provider only ever needs to script `stream()`, one
entry per turn, never `complete_json()`.
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

import tret.cli as cli_module
import tret.local_run as local_run
from tret.config import Settings
from tret.providers.base import (
    JsonCompletion,
    Msg,
    ProviderError,
    Provider,
    ProviderEvent,
    TextDelta,
    ToolCall,
    ToolCallComplete,
    TurnComplete,
    Usage,
)
from tret.providers.catalog import ModelCatalog, ModelInfo, ProviderRegistry
from tret.services.emissions import energy_accounting

# ── fixtures: a fake catalog, a fake registry, a scripted multi-turn provider ─

TARGET_MODEL = ModelInfo(
    id="anthropic/claude-sonnet-5",
    provider="anthropic",
    wire_id="claude-sonnet-5",
    display_name="anthropic/claude-sonnet-5",
    context_window=200_000,
    input_price_per_mtok=Decimal("3"),
    output_price_per_mtok=Decimal("15"),
    cost_tier="standard",
    energy_class="M",
)


class FakeCatalog(ModelCatalog):
    """A catalog seeded with fixed entries — never touches models.yaml or the
    network (`warm_once()` is a no-op: the catalog is pre-marked warmed)."""

    def __init__(self, models: list[ModelInfo]) -> None:
        self._static = {m.id: m for m in models}
        self._dynamic: dict[str, ModelInfo] = {}
        self._dynamic_fetched_at = 0.0
        self._local: dict[str, ModelInfo] = {}
        self._local_fetched_at = 0.0
        self._tool_probe_cache: dict[str, bool] = {}
        self._warmed = True


class FakeRegistry(ProviderRegistry):
    """Hands out one scripted provider for every provider name."""

    def __init__(self, provider: Provider, *, keyed: bool = True) -> None:
        self._provider = provider
        self._keyed = keyed

    def has_key(self, provider: str) -> bool:
        return self._keyed

    def get(self, provider: str) -> Provider:
        return self._provider


@dataclass
class ScriptedTurn:
    text_chunks: tuple[str, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=lambda: Usage(input_tokens=10, output_tokens=5))
    stop_reason: str = "end_turn"
    emit_turn_complete: bool = True


@dataclass
class StubProvider(Provider):
    """Scripts one `ScriptedTurn` per call to `stream()`, in order — the last
    scripted turn repeats for any call beyond the script (handy for an
    "always calls a tool" iteration-cap script)."""

    name: str = "stub"
    turns: list[ScriptedTurn] = field(default_factory=list)
    stream_calls: list[dict] = field(default_factory=list)

    async def complete_json(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        schema: dict,
        tool_name: str = "respond",
        max_tokens: int = 1024,
        timeout: float = 30.0,
    ) -> JsonCompletion:
        raise AssertionError(
            "complete_json() should never be called: every test pins the model"
        )

    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[Msg],
        tools: list,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[ProviderEvent]:
        idx = len(self.stream_calls)
        self.stream_calls.append(
            {"model": model, "system": system, "messages": list(messages), "tools": tools}
        )
        turn = self.turns[idx] if idx < len(self.turns) else self.turns[-1]
        for chunk in turn.text_chunks:
            yield TextDelta(chunk)
        for tc in turn.tool_calls:
            yield ToolCallComplete(tc)
        if turn.emit_turn_complete:
            yield TurnComplete(usage=turn.usage, stop_reason=turn.stop_reason)


def _wire(monkeypatch, provider: Provider, *, models: list[ModelInfo] | None = None):
    catalog = FakeCatalog(models or [TARGET_MODEL])
    registry = FakeRegistry(provider)
    monkeypatch.setattr(local_run, "get_catalog", lambda: catalog)
    monkeypatch.setattr(local_run, "ProviderRegistry", lambda: registry)
    return catalog, registry


def _tool_msgs(provider: StubProvider) -> list[Msg]:
    """Every role="tool" message in the final transcript.

    `messages` is one growing list the loop mutates in place — each
    `stream_calls` entry is `list(messages)`, a shallow snapshot at that
    moment, so later entries already contain everything earlier ones did.
    The last entry is the fullest transcript; reading from every entry would
    double- and triple-count the same appended messages.
    """
    if not provider.stream_calls:
        return []
    return [m for m in provider.stream_calls[-1]["messages"] if m.role == "tool"]


# ── 1. full loop: list_files, read_file, then answer; usage sums correctly ──


async def test_full_loop_uses_tools_then_answers(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hello world\nsecond line\n")

    provider = StubProvider(
        turns=[
            ScriptedTurn(
                tool_calls=(ToolCall(id="1", name="list_files", arguments={}),),
                usage=Usage(input_tokens=50, output_tokens=5),
            ),
            ScriptedTurn(
                tool_calls=(ToolCall(id="2", name="read_file", arguments={"path": "a.txt"}),),
                usage=Usage(input_tokens=60, output_tokens=5),
            ),
            ScriptedTurn(text_chunks=("Done.",), usage=Usage(input_tokens=70, output_tokens=10)),
        ]
    )
    catalog, registry = _wire(monkeypatch, provider)

    result = await local_run.arun(
        "summarize a.txt", path=str(tmp_path), model=TARGET_MODEL.id
    )

    assert result.text == "Done."
    assert result.status == "completed"
    assert result.iterations == 3
    assert result.model == TARGET_MODEL.id

    tool_msgs = _tool_msgs(provider)
    assert len(tool_msgs) == 2
    assert tool_msgs[0].tool_call_id == "1"
    assert "a.txt" in (tool_msgs[0].content or "")
    assert tool_msgs[1].tool_call_id == "2"
    assert "hello world" in (tool_msgs[1].content or "")
    assert all(not m.meta.get("error") for m in tool_msgs)

    # Summed across all three turns: (50+60+70) input, (5+5+10) output — and
    # the receipt is priced off exactly that sum (services/emissions and
    # ModelInfo.cost_usd are both linear in tokens, so summing then pricing
    # equals pricing per-turn and summing).
    expected_usd = float(TARGET_MODEL.cost_usd(180, 20, 0, 0))
    assert result.receipt.usd == expected_usd
    expected_accounting = energy_accounting(TARGET_MODEL, 180, 20, 0, 0, catalog=catalog)
    assert result.receipt.co2e_g == expected_accounting["co2e_g"]
    assert result.receipt.energy_wh == expected_accounting["energy_wh"]
    assert result.receipt.usage == {
        "input_tokens": 180,
        "output_tokens": 20,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


# ── 2. path safety: traversal, absolute path, symlink escape all refused ───


async def test_traversal_absolute_and_symlink_escape_are_refused(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope")
    (root / "escape").symlink_to(outside)

    provider = StubProvider(
        turns=[
            ScriptedTurn(
                tool_calls=(
                    ToolCall(id="1", name="read_file", arguments={"path": "../outside/secret.txt"}),
                )
            ),
            ScriptedTurn(
                tool_calls=(ToolCall(id="2", name="read_file", arguments={"path": "/etc/passwd"}),)
            ),
            ScriptedTurn(
                tool_calls=(
                    ToolCall(id="3", name="read_file", arguments={"path": "escape/secret.txt"}),
                )
            ),
            ScriptedTurn(text_chunks=("ok",)),
        ]
    )
    _wire(monkeypatch, provider)

    result = await local_run.arun("try to escape", path=str(root), model=TARGET_MODEL.id)

    # The run was not derailed by three refusals — it kept going and answered.
    assert result.text == "ok"
    assert result.status == "completed"

    tool_msgs = _tool_msgs(provider)
    assert len(tool_msgs) == 3
    assert all(m.meta.get("error") for m in tool_msgs)
    assert all("outside the run's root directory" in (m.content or "") for m in tool_msgs)


# ── 3. truncation marker + offset continuation ──────────────────────────────


async def test_read_file_truncates_and_offset_continues(tmp_path, monkeypatch):
    big_text = "x" * 25_000
    (tmp_path / "big.txt").write_text(big_text)

    provider = StubProvider(
        turns=[
            ScriptedTurn(
                tool_calls=(ToolCall(id="1", name="read_file", arguments={"path": "big.txt"}),)
            ),
            ScriptedTurn(
                tool_calls=(
                    ToolCall(
                        id="2",
                        name="read_file",
                        arguments={"path": "big.txt", "offset": 20000},
                    ),
                )
            ),
            ScriptedTurn(text_chunks=("read it all",)),
        ]
    )
    _wire(monkeypatch, provider)

    result = await local_run.arun("read big.txt", path=str(tmp_path), model=TARGET_MODEL.id)
    assert result.text == "read it all"

    tool_msgs = _tool_msgs(provider)
    first, second = tool_msgs[0].content, tool_msgs[1].content
    assert "chars 0-20000 of 25000" in first
    assert "[... 5000 more characters; call again with offset=20000]" in first
    assert "chars 20000-25000 of 25000" in second
    assert "more characters" not in second


# ── 4. iteration cap: a script that always calls a tool ────────────────────


async def test_iteration_cap_status_and_receipt(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hi")
    provider = StubProvider(
        turns=[
            ScriptedTurn(
                tool_calls=(ToolCall(id="x", name="list_files", arguments={}),),
                usage=Usage(input_tokens=10, output_tokens=2),
            )
        ]
    )
    _wire(monkeypatch, provider)

    result = await local_run.arun(
        "loop forever", path=str(tmp_path), model=TARGET_MODEL.id, max_iterations=3
    )

    assert result.status == "hit_iteration_cap"
    assert result.iterations == 3
    assert len(provider.stream_calls) == 3
    # Receipt is still built off whatever usage the run did accumulate.
    assert result.receipt.usd == float(TARGET_MODEL.cost_usd(30, 6, 0, 0))


# ── 5. ledger: entry appended, fields correct, None stays null ─────────────


async def test_ledger_entry_has_correct_fields(tmp_path, monkeypatch):
    ledger_file = tmp_path / "ledger" / "ledger.jsonl"
    monkeypatch.setattr(
        local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file))
    )

    provider = StubProvider(
        turns=[ScriptedTurn(text_chunks=("a haiku",), usage=Usage(input_tokens=10, output_tokens=5))]
    )
    _wire(monkeypatch, provider)

    result = await local_run.arun("write a haiku", model=TARGET_MODEL.id, out="out.md")

    assert ledger_file.exists()
    lines = ledger_file.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])

    assert entry["id"] == result.ledger_id
    assert entry["model"] == TARGET_MODEL.id
    assert entry["status"] == "completed"
    assert entry["iterations"] == 1
    assert entry["usd"] == result.receipt.usd
    assert entry["co2e_g"] == result.receipt.co2e_g
    assert entry["energy_wh"] == result.receipt.energy_wh
    assert entry["avoided_usd_pct"] == result.receipt.avoided_usd_pct
    assert entry["avoided_co2e_pct"] == result.receipt.avoided_co2e_pct
    assert entry["task"] == "write a haiku"
    assert entry["out"] == "out.md"
    # Pinned mode never calls the LLM router, so there is no routing overhead
    # to meter — the field stays null, not a fabricated 0.
    assert entry["overhead_usd"] is None
    assert result.receipt.overhead is None
    # A confidently metered turn, not a guess.
    assert entry["estimated"] is False
    assert result.receipt.estimated is False


async def test_ledger_entry_records_no_run_when_no_out_given(tmp_path, monkeypatch):
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(
        local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file))
    )
    provider = StubProvider(turns=[ScriptedTurn(text_chunks=("ok",))])
    _wire(monkeypatch, provider)

    await local_run.arun("no out given", model=TARGET_MODEL.id)

    entry = json.loads(ledger_file.read_text().splitlines()[0])
    assert entry["out"] is None


async def test_ledger_io_failure_warns_but_run_succeeds(tmp_path, monkeypatch, capsys):
    # The ledger's parent directory is itself a plain file, so
    # `path.parent.mkdir(parents=True, exist_ok=True)` raises FileExistsError
    # (an OSError) — deterministic across platforms, no permission games.
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    ledger_file = blocker / "ledger.jsonl"
    monkeypatch.setattr(
        local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file))
    )

    provider = StubProvider(turns=[ScriptedTurn(text_chunks=("still works",))])
    _wire(monkeypatch, provider)

    result = await local_run.arun("task", model=TARGET_MODEL.id)

    assert result.text == "still works"  # the run itself was unaffected
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower()
    assert "ledger" in captured.err.lower()


def test_default_ledger_path_is_dot_tret_in_home():
    assert local_run.DEFAULT_LEDGER_PATH == local_run.Path.home() / ".tret" / "ledger.jsonl"


# ── 6. CLI end-to-end, in-process ───────────────────────────────────────────


def test_cli_run_writes_out_file_and_prints_progress(tmp_path, monkeypatch, capsys):
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(
        local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file))
    )
    provider = StubProvider(turns=[ScriptedTurn(text_chunks=("final answer",))])
    _wire(monkeypatch, provider)

    out_path = tmp_path / "out" / "result.md"
    monkeypatch.setattr(
        "sys.argv",
        ["tret", "run", "summarize this", "--model", TARGET_MODEL.id, "--out", str(out_path)],
    )
    cli_module.main()

    assert out_path.read_text() == "final answer"
    captured = capsys.readouterr()
    assert captured.out == ""  # the answer went to the file, not stdout
    assert "written to" in captured.err
    assert "receipt" in captured.err
    assert "ledger #" in captured.err


def test_cli_run_prints_text_to_stdout_when_no_out(tmp_path, monkeypatch, capsys):
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(
        local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file))
    )
    provider = StubProvider(turns=[ScriptedTurn(text_chunks=("printed answer",))])
    _wire(monkeypatch, provider)

    monkeypatch.setattr(
        "sys.argv", ["tret", "run", "summarize this", "--model", TARGET_MODEL.id]
    )
    cli_module.main()

    captured = capsys.readouterr()
    assert captured.out.strip() == "printed answer"
    assert "receipt" in captured.err


def test_cli_run_json_emits_parseable_json_to_stdout(tmp_path, monkeypatch, capsys):
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(
        local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file))
    )
    provider = StubProvider(turns=[ScriptedTurn(text_chunks=("json answer",))])
    _wire(monkeypatch, provider)

    monkeypatch.setattr(
        "sys.argv",
        ["tret", "run", "summarize this", "--model", TARGET_MODEL.id, "--json", "--quiet"],
    )
    cli_module.main()

    captured = capsys.readouterr()
    assert captured.err == ""  # --quiet silenced progress; --json means no receipt line either
    payload = json.loads(captured.out)
    assert payload["text"] == "json answer"
    assert payload["status"] == "completed"
    assert payload["iterations"] == 1
    assert "usd" in payload["receipt"]
    assert payload["ledger_id"]


def test_cli_run_quiet_silences_progress_without_json(tmp_path, monkeypatch, capsys):
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(
        local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file))
    )
    provider = StubProvider(turns=[ScriptedTurn(text_chunks=("quiet answer",))])
    _wire(monkeypatch, provider)

    monkeypatch.setattr(
        "sys.argv", ["tret", "run", "summarize this", "--model", TARGET_MODEL.id, "--quiet"]
    )
    cli_module.main()

    captured = capsys.readouterr()
    assert captured.out.strip() == "quiet answer"
    # --quiet only silences *progress*; the receipt line is the final result
    # summary, not progress, so it still prints.
    assert "receipt" in captured.err


def test_cli_run_unknown_max_cost_tier_exits_nonzero_with_value_error_message(
    tmp_path, monkeypatch, capsys
):
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(
        local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file))
    )
    provider = StubProvider(turns=[ScriptedTurn(text_chunks=("unreachable",))])
    _wire(monkeypatch, provider)

    monkeypatch.setattr(
        "sys.argv",
        [
            "tret", "run", "summarize this",
            "--model", TARGET_MODEL.id,
            "--max-cost-tier", "not-a-tier",
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main()

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "max_cost_tier" in captured.err
    assert not provider.stream_calls  # never got as far as calling the model


def test_cli_run_missing_task_exits_nonzero_with_usage():
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "tret.cli", "run"],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(os.path.dirname(__file__)),
        timeout=30,
    )
    assert proc.returncode != 0
    assert "usage: tret run" in proc.stderr


def test_cli_run_help_exits_zero_fast():
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "tret.cli", "run", "--help"],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(os.path.dirname(__file__)),
        timeout=30,
    )
    assert proc.returncode == 0
    assert "usage: tret run" in proc.stdout


# ── 7. pdf / docx extension dispatch, extractors monkeypatched ─────────────


def test_extract_text_dispatches_pdf_to_pypdf(tmp_path, monkeypatch):
    calls = []

    def fake_pdf_extract(path):
        calls.append(path)
        return "pdf contents"

    monkeypatch.setattr(local_run, "_extract_pdf_text", fake_pdf_extract)
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"not a real pdf")

    assert local_run._extract_text(p) == "pdf contents"
    assert calls == [p]


def test_extract_text_dispatches_docx_to_python_docx(tmp_path, monkeypatch):
    calls = []

    def fake_docx_extract(path):
        calls.append(path)
        return "docx contents"

    monkeypatch.setattr(local_run, "_extract_docx_text", fake_docx_extract)
    p = tmp_path / "doc.docx"
    p.write_bytes(b"not a real docx")

    assert local_run._extract_text(p) == "docx contents"
    assert calls == [p]


def test_extract_text_falls_back_to_utf8_replace(tmp_path):
    p = tmp_path / "doc.txt"
    p.write_bytes(b"hello \xff\xfe world")  # not valid utf-8

    text = local_run._extract_text(p)
    assert "hello" in text and "world" in text  # errors="replace", never raises


async def test_read_file_tool_dispatches_pdf_through_extract_text(tmp_path, monkeypatch):
    monkeypatch.setattr(local_run, "_extract_text", lambda path: "extracted body")
    (tmp_path / "report.pdf").write_bytes(b"stand-in bytes, never really parsed")

    provider = StubProvider(
        turns=[
            ScriptedTurn(
                tool_calls=(ToolCall(id="1", name="read_file", arguments={"path": "report.pdf"}),)
            ),
            ScriptedTurn(text_chunks=("summarized",)),
        ]
    )
    _wire(monkeypatch, provider)

    result = await local_run.arun("summarize report.pdf", path=str(tmp_path), model=TARGET_MODEL.id)
    assert result.text == "summarized"
    assert "extracted body" in (_tool_msgs(provider)[0].content or "")


# ── review fixes: confinement, clobber, failure accounting ──────────────────
@dataclass
class FailingProvider(StubProvider):
    """StubProvider that raises ProviderError on stream call `fail_on_call`
    (1-based), after yielding `fail_after_chunks` text chunks."""

    fail_on_call: int = 2
    fail_after_chunks: tuple[str, ...] = ()

    async def stream(self, **kwargs) -> AsyncIterator[ProviderEvent]:
        if len(self.stream_calls) + 1 == self.fail_on_call:
            self.stream_calls.append({"messages": list(kwargs["messages"])})
            for chunk in self.fail_after_chunks:
                yield TextDelta(chunk)
            raise ProviderError("stub", "upstream 529 overloaded", status=529)
        async for event in super().stream(**kwargs):
            yield event


@pytest.mark.asyncio
async def test_search_and_list_refuse_symlinked_file_outside_root(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret_file = outside / "credentials"
    secret_file.write_text("aws_secret_access_key = SECRETVALUE123\n")
    root = tmp_path / "root"
    root.mkdir()
    (root / "notes.txt").write_text("aws_secret is mentioned here but held elsewhere\n")
    os.symlink(secret_file, root / "aws")

    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file)))
    provider = StubProvider(
        turns=[
            ScriptedTurn(tool_calls=(ToolCall(id="t1", name="list_files", arguments={}),)),
            ScriptedTurn(
                tool_calls=(
                    ToolCall(
                        id="t2",
                        name="search_files",
                        arguments={"query": "aws_secret", "max_results": 50},
                    ),
                )
            ),
            ScriptedTurn(text_chunks=("done",)),
        ]
    )
    _wire(monkeypatch, provider)

    await local_run.arun("audit", path=str(root), model=TARGET_MODEL.id)

    tool_msgs = _tool_msgs(provider)
    listing, search = tool_msgs[0].content, tool_msgs[1].content
    assert "notes.txt" in listing
    assert "aws" not in listing.replace("aws_secret", "")  # the symlink is not listed
    assert "SECRETVALUE123" not in search  # and its target is never read
    assert "notes.txt" in search  # while in-bounds matches still surface


@pytest.mark.asyncio
async def test_search_max_results_is_clamped_server_side(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "many.txt").write_text("\n".join(f"needle line {i}" for i in range(120)))
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file)))
    provider = StubProvider(
        turns=[
            ScriptedTurn(
                tool_calls=(
                    ToolCall(
                        id="t1",
                        name="search_files",
                        arguments={"query": "needle", "max_results": 1000},
                    ),
                )
            ),
            ScriptedTurn(text_chunks=("done",)),
        ]
    )
    _wire(monkeypatch, provider)

    await local_run.arun("hunt", path=str(root), model=TARGET_MODEL.id)

    hits = [line for line in _tool_msgs(provider)[0].content.splitlines() if "needle" in line]
    assert 0 < len(hits) <= 50


@pytest.mark.asyncio
async def test_read_file_refuses_dotfiles(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / ".env").write_text("TRET_ANTHROPIC_API_KEY=sk-ant-REALKEY\n")
    (root / "readme.txt").write_text("hello\n")
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file)))
    provider = StubProvider(
        turns=[
            ScriptedTurn(tool_calls=(ToolCall(id="t1", name="read_file", arguments={"path": ".env"}),)),
            ScriptedTurn(text_chunks=("done",)),
        ]
    )
    _wire(monkeypatch, provider)

    await local_run.arun("inspect", path=str(root), model=TARGET_MODEL.id)

    msg = _tool_msgs(provider)[0]
    assert msg.meta["error"] is True
    assert "sk-ant-REALKEY" not in msg.content
    assert "hidden" in msg.content


@pytest.mark.asyncio
async def test_provider_error_mid_loop_still_receipts_and_ledgers(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("alpha\n")
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file)))
    provider = FailingProvider(
        turns=[
            ScriptedTurn(
                tool_calls=(ToolCall(id="t1", name="read_file", arguments={"path": "a.txt"}),),
                usage=Usage(input_tokens=5000, output_tokens=900),
            ),
        ],
        fail_on_call=2,
    )
    _wire(monkeypatch, provider)

    result = await local_run.arun("summarize", path=str(root), model=TARGET_MODEL.id)

    assert result.status == "failed"
    assert "529" in (result.error or "")
    # Turn 1 completed and reported usage — those tokens cost money, so the
    # receipt prices them even though the run failed.
    assert result.receipt.usd is not None
    assert result.receipt.usage["input_tokens"] == 5000
    entry = json.loads(ledger_file.read_text().splitlines()[-1])
    assert entry["status"] == "failed"
    assert entry["usd"] == result.receipt.usd


@pytest.mark.asyncio
async def test_provider_error_with_streamed_text_books_an_estimated_cost(tmp_path, monkeypatch):
    """The provider was paid for the streamed text even though no TurnComplete
    ever arrived to meter it. Before this fix that turn cost nothing in the
    receipt at all — now it is priced from an ESTIMATE of what was on the wire
    and what streamed back, flagged so it is never read as a metered figure.
    """
    root = tmp_path / "root"
    root.mkdir()
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file)))
    provider = FailingProvider(
        fail_on_call=1,
        fail_after_chunks=("Some partial analysis before the connection died",),
    )
    _wire(monkeypatch, provider)

    result = await local_run.arun("summarize", path=str(root), model=TARGET_MODEL.id)

    assert result.status == "failed"
    assert "529" in (result.error or "")
    assert result.receipt.usd is not None
    assert result.receipt.usd > 0
    assert result.receipt.estimated is True
    assert result.receipt.usage["output_tokens"] > 0
    entry = json.loads(ledger_file.read_text().splitlines()[-1])
    assert entry["status"] == "failed"
    assert entry["usd"] == result.receipt.usd
    # The one field a ledger reader needs to tell this guessed-but-priced line
    # apart from an ordinary metered one.
    assert entry["estimated"] is True


@pytest.mark.asyncio
async def test_a_dying_second_turn_does_not_paper_over_a_first_turns_unreported_usage(
    tmp_path, monkeypatch
):
    """A local model whose first turn reports no usage at all (some OpenAI-
    compat servers ignore `stream_options.include_usage` entirely) is already
    an undercount before anything goes wrong. If the *second* turn then dies
    mid-stream, its estimate must not paper over that: `usage_reported` used
    to `or` in `usage_estimated` unconditionally, so a confidently-priced
    receipt came out the other side of a run that had already admitted (via
    the empty first turn) that its numbers were incomplete.
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("alpha\n")
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file)))
    provider = FailingProvider(
        turns=[
            ScriptedTurn(
                tool_calls=(ToolCall(id="t1", name="read_file", arguments={"path": "a.txt"}),),
                usage=Usage(),  # the local model reported nothing for turn 1
            ),
        ],
        fail_on_call=2,
        fail_after_chunks=("Some partial analysis before the connection died",),
    )
    _wire(monkeypatch, provider)

    result = await local_run.arun("summarize", path=str(root), model=TARGET_MODEL.id)

    assert result.status == "failed"
    # Turn 2's estimate is real (tokens were genuinely spent) — see the
    # sibling estimated-cost test — but turn 1's silence means the receipt as
    # a whole is not a reliable count, and `usd` must say so honestly rather
    # than presenting an undercount as a priced figure.
    assert result.receipt.usd is None
    entry = json.loads(ledger_file.read_text().splitlines()[-1])
    assert entry["usd"] is None


@pytest.mark.asyncio
async def test_provider_error_with_nothing_streamed_books_no_estimate(tmp_path, monkeypatch):
    """The other side of it: a `ProviderError` before a single byte streams
    back has nothing to estimate from — pricing it would fabricate a number,
    not read one off what happened (see the `Receipt` docstring)."""
    root = tmp_path / "root"
    root.mkdir()
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file)))
    provider = FailingProvider(fail_on_call=1)
    _wire(monkeypatch, provider)

    result = await local_run.arun("summarize", path=str(root), model=TARGET_MODEL.id)

    assert result.status == "failed"
    assert result.receipt.usd is None
    assert result.receipt.estimated is False


def test_cli_hit_iteration_cap_preserves_out_file_and_exits_nonzero(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("alpha\n")
    out_path = tmp_path / "report.md"
    out_path.write_text("PREVIOUS GOOD REPORT\n")
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file)))
    # Always calls a tool: the last scripted turn repeats, so the cap is hit.
    provider = StubProvider(
        turns=[
            ScriptedTurn(tool_calls=(ToolCall(id="t1", name="list_files", arguments={}),)),
        ]
    )
    _wire(monkeypatch, provider)
    monkeypatch.setattr(
        "sys.argv",
        [
            "tret", "run", "regenerate", "--path", str(root),
            "--out", str(out_path), "--model", TARGET_MODEL.id, "--max-iterations", "2",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        cli_module.main()

    assert exc.value.code == 1
    assert out_path.read_text() == "PREVIOUS GOOD REPORT\n"  # untouched
    captured = capsys.readouterr()
    assert "hit_iteration_cap" in captured.err
    assert "written to" not in captured.err


def test_cli_failed_run_prints_error_and_receipt_with_status(tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("alpha\n")
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file)))
    provider = FailingProvider(
        turns=[
            ScriptedTurn(tool_calls=(ToolCall(id="t1", name="read_file", arguments={"path": "a.txt"}),)),
        ],
        fail_on_call=2,
    )
    _wire(monkeypatch, provider)
    monkeypatch.setattr(
        "sys.argv", ["tret", "run", "summarize", "--path", str(root), "--model", TARGET_MODEL.id]
    )

    with pytest.raises(SystemExit) as exc:
        cli_module.main()

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "error: run did not complete" in captured.err
    assert "status failed" in captured.err
    assert "receipt" in captured.err  # the paid-for tokens still got a receipt
    assert ledger_file.exists()


def test_cli_out_pointing_at_directory_falls_back_to_stdout(tmp_path, monkeypatch, capsys):
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file)))
    provider = StubProvider(turns=[ScriptedTurn(text_chunks=("the answer",))])
    _wire(monkeypatch, provider)
    a_dir = tmp_path / "adir"
    a_dir.mkdir()
    monkeypatch.setattr(
        "sys.argv",
        ["tret", "run", "task", "--model", TARGET_MODEL.id, "--out", str(a_dir)],
    )

    with pytest.raises(SystemExit) as exc:
        cli_module.main()

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "could not write" in captured.err
    assert "the answer" in captured.out  # the paid-for answer is not lost


def test_cli_max_iterations_zero_rejected_at_argparse(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv", ["tret", "run", "task", "--max-iterations", "0"]
    )
    with pytest.raises(SystemExit) as exc:
        cli_module.main()
    assert exc.value.code == 2
    assert "at least 1" in capsys.readouterr().err


# ── 7. --measured-wh / measured_energy_wh: an operator's own reading ────────


async def test_arun_measured_energy_wh_replaces_the_estimate(tmp_path, monkeypatch):
    provider = StubProvider(turns=[ScriptedTurn(text_chunks=("Done.",))])
    catalog, registry = _wire(monkeypatch, provider)
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file)))

    result = await local_run.arun(
        "summarize x", model=TARGET_MODEL.id, measured_energy_wh=7.5
    )

    assert result.receipt.energy_wh == pytest.approx(7.5)
    assert result.receipt.raw["energy_source"] == "measured"
    expected_accounting = energy_accounting(
        TARGET_MODEL, 10, 5, 0, 0, catalog=catalog, measured_energy_wh=7.5
    )
    assert result.receipt.co2e_g == expected_accounting["co2e_g"]


async def test_arun_negative_measured_energy_wh_raises_value_error(tmp_path, monkeypatch):
    provider = StubProvider(turns=[ScriptedTurn(text_chunks=("Done.",))])
    _wire(monkeypatch, provider)
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file)))

    with pytest.raises(ValueError, match="measured_energy_wh"):
        await local_run.arun("summarize x", model=TARGET_MODEL.id, measured_energy_wh=-1.0)

    assert not provider.stream_calls  # rejected before ever calling the model


def test_cli_measured_wh_flag_is_recorded_as_measured(tmp_path, monkeypatch, capsys):
    ledger_file = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(local_run, "get_settings", lambda: Settings(ledger_path=str(ledger_file)))
    provider = StubProvider(turns=[ScriptedTurn(text_chunks=("metered answer",))])
    _wire(monkeypatch, provider)

    monkeypatch.setattr(
        "sys.argv",
        [
            "tret", "run", "summarize this",
            "--model", TARGET_MODEL.id,
            "--measured-wh", "5.0",
            "--json", "--quiet",
        ],
    )
    cli_module.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["receipt"]["energy_wh"] == pytest.approx(5.0)
    assert payload["receipt"]["raw"]["energy_source"] == "measured"


def test_cli_negative_measured_wh_rejected_at_argparse(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv", ["tret", "run", "task", "--measured-wh", "-1"]
    )
    with pytest.raises(SystemExit) as exc:
        cli_module.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err.lower()
    assert "measured-wh" in err or ">= 0" in err
