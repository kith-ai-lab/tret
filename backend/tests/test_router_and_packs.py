"""Deterministic router fallback + pack validation."""
from pathlib import Path

from bench.packs.loader import validate_pack
from bench.providers.catalog import ModelCatalog, ProviderRegistry
from bench.router_llm.fallback import fallback_model

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
    assert chosen == "openrouter/openai/gpt-4.1"  # first verdict pref with a key


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
    from bench.router_llm.fallback import FALLBACK_TABLE

    for t in manifest.task_types:
        assert t.shape in FALLBACK_TABLE


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
