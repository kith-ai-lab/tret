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
