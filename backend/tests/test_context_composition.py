"""Prompt composition accounting and task-scoped doctrine loading.

Two token-economy properties, tested without a DB:

* every component of the assembled context is *accounted for* — the breakdown
  the run persists adds up to the prompt that was actually sent;
* a task type loads only the doctrine it declares, and declaring nothing loads
  all of it (so existing packs are unaffected).
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from fastapi import HTTPException

from bench.api.harnesses import _validate_tool_names
from bench.db.models import Harness, Pack
from bench.engine.context import (
    assemble_context,
    composition_report,
    estimate_tokens,
    parse_doctrine_selector,
    select_doctrine_text,
    task_doctrine_selection,
    tool_spec_block,
)
from bench.engine.tools import get_builtin_tools
from bench.packs.loader import validate_pack

PACK_DIR = Path(__file__).parent.parent.parent / "packs/climate-risk"


def _pack(pack_dir: Path = PACK_DIR) -> Pack:
    """The real pack, loaded exactly as install_pack stores it, minus the DB."""
    manifest, schemas, errors = validate_pack(pack_dir)
    assert errors == []
    stored = manifest.model_dump()
    stored["schemas"] = schemas
    for task in stored["task_types"]:
        if task.get("output_schema"):
            task["output_schema_slug"] = Path(task["output_schema"]).stem.removesuffix(".schema")
    return Pack(
        slug=stored["pack"],
        version=stored["version"],
        doctrine_sha="0" * 64,
        manifest=stored,
        source_path=str(pack_dir),
    )


def _harness(**kw) -> Harness:
    kw.setdefault("name", "Test Harness")
    return Harness(model_policy={}, tool_names=[], loop_config={}, **kw)


def _context(task_type: str, pack: Pack | None = None, harness: Harness | None = None):
    pack = pack if pack is not None else _pack()
    return assemble_context(
        harness or _harness(), pack, task_type, pack.manifest["schemas"] if pack else {}
    )


def _blocks_by_kind(blocks, kind: str) -> list:
    return [b for b in blocks if b.kind == kind]


# ── composition accounting ────────────────────────────────────────────────────
def test_composition_accounts_for_every_component_of_the_prompt():
    assembled = _context("divergence_assessment")
    kinds = [b.kind for b in assembled.blocks]

    assert kinds[0] == "platform_preamble"
    assert kinds.count("doctrine") == 3
    assert "task_instructions" in kinds and "output_schema" in kinds
    # Nothing in the prompt is unaccounted for: the blocks *are* the prompt.
    assert sum(b.chars for b in assembled.blocks) + 2 * (len(assembled.blocks) - 1) == len(
        assembled.system
    )
    for block in assembled.blocks:
        assert block.text and block.text in assembled.system
        assert block.est_tokens == estimate_tokens(block.text)


def test_composition_report_is_the_persisted_shape():
    pack = _pack()
    assembled = _context("divergence_assessment", pack=pack)
    builtins = get_builtin_tools()
    task = next(t for t in pack.manifest["task_types"] if t["slug"] == "divergence_assessment")
    specs = [builtins[n] for n in task["tools"]]

    report = composition_report([*assembled.blocks, tool_spec_block(specs)])

    assert report["estimator"] == "chars/4"
    assert report["total_est_tokens"] == sum(b["est_tokens"] for b in report["blocks"])
    assert report["by_kind"]["doctrine"] == sum(
        b.est_tokens for b in _blocks_by_kind(assembled.blocks, "doctrine")
    )
    assert report["by_kind"]["tool_specs"] > 0
    # Every doctrine block carries the hash of what was actually loaded.
    for block in report["blocks"]:
        if block["kind"] == "doctrine":
            assert len(block["sha256"]) == 64
    tools_block = next(b for b in report["blocks"] if b["kind"] == "tool_specs")
    assert set(tools_block["parts"]) == set(task["tools"])
    # Persisted as-is on the run: JSON-serializable, no objects.
    assert json.loads(json.dumps(report)) == report


def test_harness_extra_and_capability_catalog_are_accounted_separately():
    harness = _harness(system_prompt_extra="Extra house style rules.")
    assembled = assemble_context(
        harness, None, "chat", {}, extra_context="## Capability catalog\n\n- one task"
    )
    kinds = [b.kind for b in assembled.blocks]
    assert kinds == ["platform_preamble", "task_instructions", "extra_context", "harness_extra"]
    assert "Extra house style rules." in assembled.system


# ── doctrine scoping ──────────────────────────────────────────────────────────
def test_no_declaration_loads_every_doctrine_file():
    pack = _pack()
    assembled = _context("divergence_assessment", pack=pack)
    loaded = [b.label for b in _blocks_by_kind(assembled.blocks, "doctrine")]
    assert loaded == pack.manifest["doctrine"]
    for block in _blocks_by_kind(assembled.blocks, "doctrine"):
        assert block.sections is None  # whole files
        assert 'sections="' not in block.text


def test_declared_doctrine_is_the_only_doctrine_loaded():
    assembled = _context("evidence_extraction")
    loaded = [b.label for b in _blocks_by_kind(assembled.blocks, "doctrine")]
    assert loaded == ["doctrine/01-assessment-principles.md"]
    assert "Divergence Assessment Procedure" not in assembled.system
    assert "Only retrieved values may be cited" in assembled.system


def test_scoping_a_task_shrinks_its_prompt_but_not_another_task_s():
    full = _context("divergence_assessment")
    scoped = _context("evidence_extraction")
    full_doctrine = sum(b.est_tokens for b in _blocks_by_kind(full.blocks, "doctrine"))
    scoped_doctrine = sum(b.est_tokens for b in _blocks_by_kind(scoped.blocks, "doctrine"))
    assert scoped_doctrine < full_doctrine
    # The unscoped task still gets every section it may need to cite.
    assert "## Step 6 — Issue the verdict" in full.system
    assert "## outdated_inputs" in full.system


def test_section_selector_loads_the_section_and_the_file_s_framing():
    text = (PACK_DIR / "doctrine/03-reason-codes.md").read_text()
    loaded, matched, unresolved = select_doctrine_text(text, ["outdated_inputs"])
    assert unresolved == []
    assert matched == ["outdated_inputs"]
    assert "## outdated_inputs" in loaded
    assert "## scale_mismatch" not in loaded
    # Front matter rides along: the title plus the rule that makes the section
    # interpretable ("each code has an evidence test").
    assert loaded.startswith("# Divergence Reason Codes")
    assert "evidence test" in loaded
    assert len(loaded) < len(text)


def test_multiple_sections_come_back_in_document_order():
    text = (PACK_DIR / "doctrine/02-divergence-procedure.md").read_text()
    loaded, matched, unresolved = select_doctrine_text(
        text, ["Step 6", "Step 3 — Check signal robustness"]
    )
    assert unresolved == []
    assert matched == ["Step 6 — Issue the verdict", "Step 3 — Check signal robustness"]
    assert loaded.index("## Step 3") < loaded.index("## Step 6")
    assert "## Step 5" not in loaded


def test_a_top_level_selector_is_the_whole_document():
    text = (PACK_DIR / "doctrine/03-reason-codes.md").read_text()
    loaded, _, unresolved = select_doctrine_text(text, ["Divergence Reason Codes"])
    assert unresolved == []
    assert loaded == text


def test_an_unresolvable_section_fails_open_to_the_whole_file():
    """Never silently starve a task of doctrine: more context, and say so."""
    text = (PACK_DIR / "doctrine/03-reason-codes.md").read_text()
    loaded, _, unresolved = select_doctrine_text(text, ["No Such Heading"])
    assert unresolved == ["No Such Heading"]
    assert loaded == text


def test_selection_helpers_handle_selector_syntax():
    assert parse_doctrine_selector("a.md#Some Heading") == ("a.md", "Some Heading")
    assert parse_doctrine_selector("a.md") == ("a.md", None)
    pack_doctrine = ["a.md", "b.md"]
    assert task_doctrine_selection(pack_doctrine, None) == [("a.md", []), ("b.md", [])]
    assert task_doctrine_selection(pack_doctrine, ["b.md#One", "b.md#Two"]) == [
        ("b.md", ["One", "Two"])
    ]
    # A whole-file declaration wins over sections of the same file.
    assert task_doctrine_selection(pack_doctrine, ["b.md#One", "b.md"]) == [("b.md", [])]
    # A task can never pull in a file the pack does not list as doctrine.
    assert task_doctrine_selection(pack_doctrine, ["../secrets.md", "a.md"]) == [("a.md", [])]


def test_scoped_doctrine_hash_records_exactly_what_was_loaded():
    pack = _pack()
    full = _blocks_by_kind(_context("divergence_assessment", pack=pack).blocks, "doctrine")
    scoped = _blocks_by_kind(_context("tcfd_section_draft", pack=pack).blocks, "doctrine")
    by_label_full = {b.label: b for b in full}
    for block in scoped:
        # Same file, same bytes loaded → same hash; the audit trail is honest
        # about which files a scoped run did *not* see.
        assert block.sha256 == by_label_full[block.label].sha256
    assert {b.label for b in scoped} < {b.label for b in full}


# ── pack validation of declarations ───────────────────────────────────────────
def _copy_pack(tmp_path: Path) -> Path:
    target = tmp_path / "climate-risk"
    shutil.copytree(PACK_DIR, target)
    return target


def test_validate_rejects_a_doctrine_file_not_in_the_pack_doctrine_list(tmp_path):
    pack_dir = _copy_pack(tmp_path)
    manifest = pack_dir / "pack.yaml"
    manifest.write_text(
        manifest.read_text().replace(
            "    doctrine:\n      - doctrine/01-assessment-principles.md\n",
            "    doctrine:\n      - templates/tcfd-assessment.md\n",
            1,
        )
    )
    _, _, errors = validate_pack(pack_dir)
    assert any("not in the pack's doctrine list" in e for e in errors)


def test_validate_rejects_a_section_that_does_not_exist(tmp_path):
    pack_dir = _copy_pack(tmp_path)
    manifest = pack_dir / "pack.yaml"
    manifest.write_text(
        manifest.read_text().replace(
            "      - doctrine/01-assessment-principles.md\n",
            '      - "doctrine/01-assessment-principles.md#Nonexistent Heading"\n',
            1,
        )
    )
    _, _, errors = validate_pack(pack_dir)
    assert any("names a heading that does not exist" in e for e in errors)


def test_the_shipped_climate_pack_declarations_all_resolve():
    pack = _pack()
    for task in pack.manifest["task_types"]:
        for block in _blocks_by_kind(_context(task["slug"], pack=pack).blocks, "doctrine"):
            assert block.note is None, block.note


# ── harness tool names are validated on write ─────────────────────────────────
# The pack-level counterpart is above (`validate_pack` rejects a task whose
# `terminal_tool`/`tools` name something the engine has no builtin for). A
# harness is the *other* place a tool name is written by hand, and it went
# unchecked: the engine offers only the tools it can resolve, so a typo silently
# removed a capability instead of failing, and the builder UI echoed the bad name
# straight back.
def test_a_harness_tool_name_with_no_builtin_is_rejected():
    with pytest.raises(HTTPException) as exc:
        _validate_tool_names(["read_document", "read_documents"])
    assert exc.value.status_code == 422
    # Names the offender, and lists what is actually available.
    assert "read_documents" in exc.value.detail
    assert "read_document" in exc.value.detail
    # The valid name in the same list is not reported as unknown.
    assert exc.value.detail.count("read_documents") == 1


def test_every_real_builtin_is_accepted_and_an_empty_list_is_fine():
    _validate_tool_names([])
    _validate_tool_names(sorted(get_builtin_tools()))


def test_duplicate_tool_names_are_left_alone():
    """Harmless — the engine builds a spec list, and a repeat is not evidence of
    the mistake an unknown name is."""
    _validate_tool_names(["read_document", "read_document"])


def test_the_seeded_harnesses_name_only_real_tools():
    """The bootstrap's own harnesses must satisfy the rule the API now enforces.

    A seeded harness naming a tool the engine dropped would fail its very first
    run under the loud run-time guard, on a fresh install, before an operator had
    touched anything. Parsed out of the source rather than by booting the
    bootstrap, which needs a database.
    """
    source = (Path(__file__).parent.parent / "bench/services/bootstrap.py").read_text()
    builtins = set(get_builtin_tools())
    seeded: set[str] = set()
    for literal in re.findall(r"tool_names=\[(.*?)\]", source, re.DOTALL):
        seeded |= set(re.findall(r'"([^"]+)"', literal))
    assert seeded, "no seeded tool_names found — has bootstrap.py changed shape?"
    assert seeded <= builtins, f"bootstrap seeds unknown tool(s): {sorted(seeded - builtins)}"
