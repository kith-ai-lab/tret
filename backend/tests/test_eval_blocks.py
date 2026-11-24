"""Block segmentation, annotation, the verbatim cutter and windows (tret/evaluation/blocks.py),
and module-set loading (tret/evaluation/modules.py)."""
from __future__ import annotations

import pytest

from tret.evaluation import blocks as blk
from tret.evaluation.modules import ModuleSetError, load_module_set, parse_module_set

MD = """## Page 1
# Annual report 2024

We make widgets.
Second line of the paragraph.

## Page 2
Physical risks: floods threaten our plants.

| Site | Risk |
| --- | --- |
| A | flood |
| B | heat |

Adaptation: we built walls.

## Page 3
Targets: reduce 42% by 2030.
"""

ESEF = "## Section 1: Governance\nThe board meets.\n\n## Section 2: Targets\nNet zero.\n"


def test_ids_and_kinds():
    seg = blk.segment(MD)
    assert [b.id for b in seg.blocks] == ["p1.b1", "p1.b2", "p2.b1", "p2.b2", "p2.b3", "p3.b1"]
    assert [b.kind for b in seg.blocks] == ["heading", "paragraph", "paragraph", "table", "paragraph", "paragraph"]
    assert seg.by_id["p1.b2"].text == "We make widgets.\nSecond line of the paragraph."
    assert seg.page_of("p3.b1") == 3


def test_table_is_one_block_with_all_rows():
    t = blk.segment(MD).by_id["p2.b2"].text
    assert t.splitlines()[0] == "| Site | Risk |" and t.count("\n") == 3


def test_section_units():
    seg = blk.segment(ESEF)
    assert [b.id for b in seg.blocks] == ["s1.b1", "s2.b1"]


def test_deterministic():
    assert blk.segment(MD).by_id.keys() == blk.segment(MD).by_id.keys()
    assert blk.annotate(MD) == blk.annotate(MD)
    assert [b.text for b in blk.segment(MD).blocks] == [b.text for b in blk.segment(MD).blocks]


def test_annotate_prefixes_blocks_and_keeps_tables_valid():
    out = blk.annotate(MD)
    assert "[p1.b2] We make widgets." in out
    assert "[p2.b2]\n| Site | Risk |" in out
    assert "## Page 2" in out
    only = blk.annotate(MD, pages=["p3"])
    assert "p3.b1" in only and "p2.b1" not in only


def test_cut_is_verbatim_across_pages_and_drops_markers():
    text = blk.cut(MD, "p2.b3", "p3.b1")
    assert text == "Adaptation: we built walls.\n\nTargets: reduce 42% by 2030."
    whole = blk.cut(MD, "p2.b1", "p2.b3")
    assert "| A | flood |" in whole and "## Page" not in whole and "[p2" not in whole
    for b in blk.segment(MD).blocks:  # every block's text is a substring of the source
        assert b.text in MD


def test_cut_rejects_bad_ids_and_backwards_spans():
    with pytest.raises(blk.BlockError):
        blk.cut(MD, "p9.b1", "p9.b2")
    with pytest.raises(blk.BlockError):
        blk.cut(MD, "p3.b1", "p1.b1")
    assert blk.cut(MD, "p3.b1", "p3.b1") == "Targets: reduce 42% by 2030."


def test_windows_are_page_aligned_and_cover_every_page():
    pages = "\n\n".join(f"## Page {i}\n" + ("word " * 200) for i in range(1, 11))
    wins = blk.windows(pages, 600)
    assert len(wins) > 1
    covered = [u for w in wins for u in w.units]
    assert covered == [f"p{i}" for i in range(1, 11)]
    assert all(w.text.count("## Page") == len(w.units) for w in wins)
    assert len(blk.windows(pages, 10**6)) == 1
    # a page bigger than the limit is a window of its own
    assert [w.units for w in blk.windows("## Page 1\n" + "x " * 5000 + "\n\n## Page 2\nshort", 100)] == [["p1"], ["p2"]]


# ── module sets ─────────────────────────────────────────────────────────────────
def test_default_module_set_has_the_four_modules():
    ms = load_module_set()
    assert ms.slugs == ["physical_climate_risk", "targets_and_transition", "governance_and_capex", "sensitive_framing"]
    assert ms.ident == "kith-climate@0.1"
    assert all(m.include and m.exclude and m.max_words == 4000 for m in ms.modules)


def test_module_set_file_via_setting(tmp_path, monkeypatch):
    from tret.config import get_settings

    f = tmp_path / "set.yaml"
    f.write_text("module_set: mine\nversion: '2'\nmodules:\n  - slug: only_one\n    title: T\n    definition: D\n")
    monkeypatch.setattr(get_settings(), "disclosure_module_set", str(f))
    ms = load_module_set()
    assert ms.ident == "mine@2" and ms.modules[0].max_words == 4000


@pytest.mark.parametrize(
    "text",
    [
        "- not a mapping",
        "module_set: x\nversion: 1\nmodules: []",
        "module_set: x\nversion: 1\nmodules:\n  - {slug: a, title: T, definition: D}\n  - {slug: a, title: T, definition: D}",
        "module_set: x\nversion: 1\nmodules:\n  - {slug: 'Bad Slug', title: T, definition: D}",
        "module_set: x\nversion: 1\nmodules:\n  - {slug: ok_slug, title: T, definition: D, surprise: 1}",
    ],
)
def test_invalid_module_sets_are_rejected(text):
    with pytest.raises(ModuleSetError):
        parse_module_set(text)


def test_missing_module_set_file(tmp_path):
    with pytest.raises(ModuleSetError):
        load_module_set(tmp_path / "nope.yaml")


def test_repeated_and_backwards_page_markers_are_text_not_boundaries():
    md = "## Page 2\nAlpha\n\n## Page 3\nBeta\n\n## Page 2\nInjected\n"
    # page numbering starts at 1: the first `## Page 2` is not sequential either
    seg = blk.segment("## Page 1\nx\n\n" + md)
    assert [u.key for u in seg.units] == ["p1", "p2", "p3"]
    p3 = [b.text for b in seg.blocks if b.unit == "p3"]
    assert p3 == ["Beta", "## Page 2", "Injected"]
    # a cut can never cross into an earlier page: ids stay inside their contiguous unit
    assert blk.cut("## Page 1\nx\n\n" + md, "p2.b1", "p3.b1") == "Alpha\n\nBeta"
    for u in seg.units:
        idx = [b.index for b in u.blocks]
        assert idx == list(range(idx[0], idx[0] + len(idx)))


def test_section_numbers_are_separate_from_pages():
    seg = blk.segment(ESEF)
    assert seg.page_of("s2.b1") is None and seg.section_of("s2.b1") == 2
    assert seg.page_of("s1.b1") is None
    p = blk.segment(MD)
    assert p.page_of("p3.b1") == 3 and p.section_of("p3.b1") is None


def test_an_oversized_unit_is_split_at_block_boundaries_and_everything_is_covered():
    page = "## Page 1\n" + "\n\n".join(("para%d " % i) * 150 for i in range(8)) + "\n\n## Page 2\nshort\n"
    seg = blk.segment(page)
    wins = blk.windows(page, 500, seg=seg)
    assert len(wins) > 2 and not any(w.oversized for w in wins)
    covered = [i for w in wins for i in range(w.first_index, w.last_index + 1)]
    assert covered == list(range(len(seg.blocks)))
    assert "(continued)" in wins[1].text and all(w.est_tokens <= 520 for w in wins)


def test_a_single_block_larger_than_the_window_is_flagged_oversized():
    md = "## Page 1\n" + "word " * 5000 + "\n\n## Page 2\nshort\n"
    wins = blk.windows(md, 200)
    assert [w.oversized for w in wins] == [True, False]
    assert wins[0].contains(blk.segment(md).by_id["p1.b1"]) and not wins[0].contains(blk.segment(md).by_id["p2.b1"])


def test_module_set_version_must_be_a_string():
    with pytest.raises(ModuleSetError, match="quoted string"):
        parse_module_set("module_set: x\nversion: 1.10\nmodules:\n  - {slug: ab, title: T, definition: D}")
    assert parse_module_set('module_set: x\nversion: "1.10"\nmodules:\n  - {slug: ab, title: T, definition: D}').version == "1.10"
