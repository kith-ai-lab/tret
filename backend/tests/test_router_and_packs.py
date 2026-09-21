"""Deterministic router fallback + pack validation."""
from pathlib import Path

from tret.packs.loader import validate_pack
from tret.providers.catalog import ModelCatalog, ProviderRegistry
from tret.router_llm.fallback import fallback_model

PACKS_DIR = Path(__file__).parent.parent.parent / "packs"


class FakeRegistry(ProviderRegistry):
    def __init__(self, providers: set[str]):
        self._providers = providers

    def has_key(self, provider: str) -> bool:
        return provider in self._providers


def test_fallback_prefers_shape_table_order():
    catalog = ModelCatalog()
    registry = FakeRegistry({"openrouter"})
    chosen = fallback_model("verdict", catalog, registry)
    assert chosen == "openrouter/openai/gpt-5.6-terra"  # first verdict pref with a key


def test_fallback_respects_allowed_list():
    catalog = ModelCatalog()
    registry = FakeRegistry({"openrouter", "anthropic", "kimi"})
    chosen = fallback_model("verdict", catalog, registry, allowed=["kimi/kimi-k2"])
    assert chosen == "kimi/kimi-k2"


def test_fallback_none_when_no_keys():
    catalog = ModelCatalog()
    registry = FakeRegistry(set())
    assert fallback_model("drafting", catalog, registry) is None


def test_climate_pack_validates():
    manifest, schemas, errors = validate_pack(PACKS_DIR / "climate-risk")
    assert errors == []
    assert manifest.pack == "climate-risk"
    assert {t.slug for t in manifest.task_types} == {
        "divergence_assessment",
        "evidence_extraction",
        "tcfd_section_draft",
        "qa_review",
    }
    assert set(schemas) == {"divergence_verdict", "evidence_finding", "qa_assessment"}
    # Every task shape must be a router fallback key.
    from tret.router_llm.fallback import FALLBACK_TABLE

    for t in manifest.task_types:
        assert t.shape in FALLBACK_TABLE


def test_every_shipped_task_offers_its_own_terminal_tool():
    """A terminal tool the task never offers can never be called.

    The run would nudge once and then end `completed_without_output` for ever,
    silently and only on that task type — which is precisely how
    `record_finding` not setting the terminal flag stayed invisible for two of
    the four shipped task types. Assert the invariant on the shipped pack, not
    just the validator.
    """
    manifest, _, errors = validate_pack(PACKS_DIR / "climate-risk")
    assert errors == []
    declared = [t for t in manifest.task_types if t.terminal_tool]
    assert len(declared) == 4  # all four shipped tasks declare one
    for task in declared:
        assert task.terminal_tool in task.tools, (
            f"task '{task.slug}' declares terminal_tool '{task.terminal_tool}' "
            f"but only offers {task.tools}"
        )


def test_terminal_tool_missing_from_the_task_tools_fails_validation(tmp_path):
    """A typo'd or forgotten terminal tool must fail at install, loudly."""
    (tmp_path / "doctrine").mkdir()
    (tmp_path / "doctrine" / "01.md").write_text("# Rules\n")
    (tmp_path / "pack.yaml").write_text(
        "pack: typo\nversion: 0.1.0\ndisplay_name: Typo\n"
        "doctrine: [doctrine/01.md]\n"
        "task_types:\n"
        "  - slug: t1\n    display_name: T1\n    shape: verdict\n"
        "    terminal_tool: record_finding\n"
        "    tools: [lookup_dataset, record_verdict]\n"
    )
    _, _, errors = validate_pack(tmp_path)
    joined = " ".join(errors)
    assert "terminal_tool 'record_finding' is not in the task's tools" in joined
    assert "the model could never call it" in joined
    # It is a known builtin, so the pre-existing check must NOT be what fired.
    assert "is not a known tool" not in joined


def test_broken_pack_reports_errors(tmp_path):
    (tmp_path / "pack.yaml").write_text(
        "pack: broken\nversion: 0.1.0\ndisplay_name: Broken\n"
        "doctrine: [doctrine/missing.md]\n"
        "task_types:\n"
        "  - slug: t1\n    display_name: T1\n    shape: verdict\n"
        "    output_schema: schemas/missing.json\n    terminal_tool: not_a_tool\n"
    )
    manifest, schemas, errors = validate_pack(tmp_path)
    joined = " ".join(errors)
    assert "doctrine file missing" in joined
    assert "schema file missing" in joined
    assert "not_a_tool" in joined


# ── harness presets (`harnesses:`) ───────────────────────────────────────────


def test_climate_pack_ships_a_valid_harness_preset():
    """The Climate Analyst harness now arrives via the pack's own preset
    (`packs/loader.py::install_pack`) rather than being hardcoded in
    `services/workspace.py` — pin the preset's exact shape here so a future
    edit to the pack notices if it drifts from what the harness install path
    (and the frontend builder, which reads this same manifest field back
    through `api/packs.py`) expects."""
    manifest, _, errors = validate_pack(PACKS_DIR / "climate-risk")
    assert errors == []
    assert [h.name for h in manifest.harnesses] == ["Climate Analyst"]
    preset = manifest.harnesses[0]
    assert preset.task_types == ["divergence_assessment"]
    assert preset.tools == []
    assert preset.suggested_cost_tier == "premium"


def _minimal_pack_yaml_with_preset(*, task_types: str, tools: str, tier: str = "") -> str:
    tier_line = f"    suggested_cost_tier: {tier}\n" if tier else ""
    return (
        "pack: preset-test\nversion: 0.1.0\ndisplay_name: Preset Test\n"
        "task_types:\n"
        "  - slug: t1\n    display_name: T1\n    shape: freeform\n"
        "harnesses:\n"
        "  - name: My Harness\n"
        f"    task_types: [{task_types}]\n"
        f"    tools: [{tools}]\n"
        f"{tier_line}"
    )


def test_harness_preset_with_unknown_tool_fails_validation(tmp_path):
    (tmp_path / "pack.yaml").write_text(
        _minimal_pack_yaml_with_preset(task_types="t1", tools="not_a_real_tool")
    )
    _, _, errors = validate_pack(tmp_path)
    joined = " ".join(errors)
    assert "harness preset 'My Harness'" in joined
    assert "unknown tool 'not_a_real_tool'" in joined


def test_harness_preset_with_unknown_task_type_slug_fails_validation(tmp_path):
    (tmp_path / "pack.yaml").write_text(
        _minimal_pack_yaml_with_preset(task_types="no_such_slug", tools="lookup_dataset")
    )
    _, _, errors = validate_pack(tmp_path)
    joined = " ".join(errors)
    assert "harness preset 'My Harness'" in joined
    assert "task_type 'no_such_slug'" in joined
    assert "not one of this pack's own task_types" in joined


def test_harness_preset_with_bad_cost_tier_fails_validation(tmp_path):
    (tmp_path / "pack.yaml").write_text(
        _minimal_pack_yaml_with_preset(task_types="t1", tools="lookup_dataset", tier="not_a_tier")
    )
    _, _, errors = validate_pack(tmp_path)
    joined = " ".join(errors)
    assert "harness preset 'My Harness'" in joined
    assert "suggested_cost_tier must be one of" in joined


def test_harness_preset_with_more_than_one_task_type_fails_validation(tmp_path):
    (tmp_path / "pack.yaml").write_text(
        "pack: preset-test\nversion: 0.1.0\ndisplay_name: Preset Test\n"
        "task_types:\n"
        "  - slug: t1\n    display_name: T1\n    shape: freeform\n"
        "  - slug: t2\n    display_name: T2\n    shape: freeform\n"
        "harnesses:\n"
        "  - name: My Harness\n"
        "    task_types: [t1, t2]\n"
        "    tools: [lookup_dataset]\n"
    )
    _, _, errors = validate_pack(tmp_path)
    joined = " ".join(errors)
    assert "harness preset 'My Harness'" in joined
    assert "task_profile is a single value" in joined


def test_valid_harness_preset_passes_validation(tmp_path):
    (tmp_path / "pack.yaml").write_text(
        _minimal_pack_yaml_with_preset(task_types="t1", tools="lookup_dataset, read_document", tier="standard")
    )
    manifest, _, errors = validate_pack(tmp_path)
    assert errors == []
    assert manifest.harnesses[0].name == "My Harness"
    assert manifest.harnesses[0].tools == ["lookup_dataset", "read_document"]
    assert manifest.harnesses[0].suggested_cost_tier == "standard"


# ── harness preset names: uniqueness + non-blank ────────────────────────────


def test_duplicate_harness_preset_names_fail_validation(tmp_path):
    (tmp_path / "pack.yaml").write_text(
        "pack: preset-test\nversion: 0.1.0\ndisplay_name: Preset Test\n"
        "task_types:\n"
        "  - slug: t1\n    display_name: T1\n    shape: freeform\n"
        "harnesses:\n"
        "  - name: My Harness\n    task_types: [t1]\n    tools: [lookup_dataset]\n"
        "  - name: My Harness\n    task_types: [t1]\n    tools: [read_document]\n"
    )
    _, _, errors = validate_pack(tmp_path)
    joined = " ".join(errors)
    assert "duplicate harness preset name 'My Harness'" in joined


def test_harness_preset_blank_name_fails_validation(tmp_path):
    (tmp_path / "pack.yaml").write_text(
        _minimal_pack_yaml_with_preset(task_types="t1", tools="lookup_dataset").replace(
            "name: My Harness", "name: ''"
        )
    )
    manifest, _, errors = validate_pack(tmp_path)
    assert manifest is None
    assert any("must not be blank" in e for e in errors)


def test_harness_preset_whitespace_only_name_fails_validation(tmp_path):
    (tmp_path / "pack.yaml").write_text(
        _minimal_pack_yaml_with_preset(task_types="t1", tools="lookup_dataset").replace(
            "name: My Harness", "name: '   '"
        )
    )
    manifest, _, errors = validate_pack(tmp_path)
    assert manifest is None
    assert any("must not be blank" in e for e in errors)


def test_harness_preset_name_is_stripped_of_surrounding_whitespace(tmp_path):
    (tmp_path / "pack.yaml").write_text(
        _minimal_pack_yaml_with_preset(task_types="t1", tools="lookup_dataset").replace(
            "name: My Harness", "name: '  My Harness  '"
        )
    )
    manifest, _, errors = validate_pack(tmp_path)
    assert errors == []
    assert manifest.harnesses[0].name == "My Harness"


# ── harness presets may not hijack the chat front door ──────────────────────


def test_harness_preset_naming_chat_task_type_is_rejected_as_a_front_door_hijack(tmp_path):
    """`install_pack` assigns `Harness.task_profile` straight from a preset's
    single `task_types` slug, and `api/chat.py` picks the workspace's chat
    harness by querying `task_profile == 'chat'` — a preset naming 'chat'
    would install a pack-authored harness that silently becomes the
    workspace's own chat front door."""
    (tmp_path / "pack.yaml").write_text(
        "pack: preset-test\nversion: 0.1.0\ndisplay_name: Preset Test\n"
        "task_types:\n"
        "  - slug: chat\n    display_name: Chat\n    shape: freeform\n"
        "harnesses:\n"
        "  - name: My Harness\n    task_types: [chat]\n    tools: [lookup_dataset]\n"
    )
    _, _, errors = validate_pack(tmp_path)
    joined = " ".join(errors)
    assert "harness preset 'My Harness'" in joined
    assert "chat" in joined
    assert "front-door" in joined or "front door" in joined
    # The pack *does* declare a 'chat' task type — this must not also trip the
    # ordinary "unknown task_type" check.
    assert "not one of this pack's own task_types" not in joined


def test_pack_may_not_declare_the_reserved_subagent_task_type(tmp_path):
    """Unlike 'chat', the slug itself is the vector: a pack-declared 'subagent'
    task type would replace the engine's subagent preamble with pack
    instructions and, by naming a terminal tool, switch off the grounding check
    a parent relies on — for every ad-hoc subagent of a run bound to the pack."""
    (tmp_path / "pack.yaml").write_text(
        "pack: preset-test\nversion: 0.1.0\ndisplay_name: Preset Test\n"
        "task_types:\n"
        "  - slug: subagent\n    display_name: Sub\n    shape: freeform\n"
    )
    _, _, errors = validate_pack(tmp_path)
    assert any("task 'subagent'" in e and "reserved" in e for e in errors)


def test_harness_preset_naming_the_subagent_profile_is_rejected(tmp_path):
    """A preset whose first task_type is 'subagent' would install a harness
    with `task_profile='subagent'` — created before the workspace's own seeded
    one, so every ad-hoc subagent would run on the pack's prompt and policy."""
    (tmp_path / "pack.yaml").write_text(
        "pack: preset-test\nversion: 0.1.0\ndisplay_name: Preset Test\n"
        "task_types:\n"
        "  - slug: subagent\n    display_name: Sub\n    shape: freeform\n"
        "harnesses:\n"
        "  - name: My Harness\n    task_types: [subagent]\n    tools: [lookup_dataset]\n"
    )
    _, _, errors = validate_pack(tmp_path)
    joined = " ".join(errors)
    assert "harness preset 'My Harness'" in joined and "Subagent harness" in joined


def test_pack_may_declare_a_chat_task_type_as_long_as_no_preset_references_it(tmp_path):
    """Task types named 'chat' may exist for other purposes — the hijack
    vector is a *preset* routing a harness's task_profile to 'chat', not the
    slug's mere existence in the pack."""
    (tmp_path / "pack.yaml").write_text(
        "pack: preset-test\nversion: 0.1.0\ndisplay_name: Preset Test\n"
        "task_types:\n"
        "  - slug: chat\n    display_name: Chat\n    shape: freeform\n"
        "  - slug: t1\n    display_name: T1\n    shape: freeform\n"
        "harnesses:\n"
        "  - name: My Harness\n    task_types: [t1]\n    tools: [lookup_dataset]\n"
    )
    _, _, errors = validate_pack(tmp_path)
    assert errors == []
