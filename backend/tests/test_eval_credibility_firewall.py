"""Engine 2's firewall: import lints, payload audit, digest, blind-both-ways, no learning leak."""
# ruff: noqa: F811
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.eval_credibility_support import *  # noqa: F401,F403
from tests.eval_credibility_support import (EXPERIMENT, body, create, env, fetch, get, reviews, seed_reports, start, world)  # noqa: F401
from tests.test_route_jury import no_router_call  # noqa: F401
from tret.db.models import DisclosureReport, RunOutcome
from tret.evaluation import credibility as cr
from tret.evaluation import sealed
from tret.storage import get_store

EVAL = Path(cr.__file__).parent


def targets(path: Path) -> list[str]:
    """Every module an import in the file reaches, fully qualified: relative imports resolved by `level`, and
    `from package import name` counted as package.name (it may be a module). Dynamic imports fail the test."""
    pkg = "tret.evaluation".split(".")
    out: list[str] = []
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.Import):
            out += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            base = ".".join(pkg[: len(pkg) - n.level + 1]) if n.level else ""
            mod = ".".join(x for x in (base, n.module or "") if x)
            out.append(mod)
            out += [f"{mod}.{a.name}" for a in n.names]
        elif isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
            assert name not in ("__import__", "import_module", "reload"), f"dynamic import in {path.name}"
        elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "importlib":
            raise AssertionError(f"importlib used in {path.name}")
    return out


SEALED_STDLIB = {"__future__", "hashlib", "re", "uuid", "dataclasses"}


def test_sealed_imports_are_an_allowlist():
    for mod in targets(EVAL / "sealed.py"):
        top = mod.split(".")[0]
        ok = top in SEALED_STDLIB or mod.startswith("sqlalchemy") or mod in ("tret.storage", "tret.storage.get_store", "tret.db.models", "tret.db.models.DisclosureReport")
        assert ok, mod
    src = (EVAL / "sealed.py").read_text()
    assert "ReportExtraction" not in src
    import inspect
    assert list(inspect.signature(sealed.credibility_payload).parameters) == ["db", "report_id", "max_input_tokens", "store"]


DENY = ("tournament ratings pools pairing juror anchors diagnostics extract density review modules blocks estimate").split()


def denied(mod: str) -> bool:
    if any(mod == f"tret.evaluation.{d}" or mod.startswith(f"tret.evaluation.{d}.") for d in DENY):
        return True
    return mod.startswith(("tret.services.peer_taxonomy", "tret.registers", "tret.engine.harness", "tret.engine.tools")) or (
        mod.startswith("tret.batches") and mod not in ("tret.batches", "tret.batches.rubrics") and not mod.startswith("tret.batches.rubrics."))


def test_runner_imports_are_a_denylist():
    for mod in targets(EVAL / "credibility.py"):
        assert not denied(mod), mod


def test_lint_catches_what_it_should(tmp_path):
    bad = tmp_path / "x.py"
    for src in ("from tret import evaluation\nfrom tret.evaluation import tournament", "import importlib\nimportlib.import_module('x')", "__import__('x')"):
        bad.write_text(src)
        try:
            hit = any(denied(m) for m in targets(bad))
        except AssertionError:
            hit = True
        assert hit or "evaluation\nfrom" in src and any(denied(m) for m in targets(bad)), src
    assert denied("tret.evaluation.tournament") and not denied("tret.evaluation.jury")


def test_engine_1_never_sees_engine_2():
    for name in "tournament juror jury pairing ratings pools anchors diagnostics".split():
        src = (EVAL / f"{name}.py").read_text()
        for mod in targets(EVAL / f"{name}.py"):
            assert not mod.endswith((".credibility", ".sealed")) and "evaluation.credibility" not in mod and "evaluation.sealed" not in mod, (name, mod)
        assert "credibility_reviews" not in src and "CredibilityReview" not in src and "CredibilityPass" not in src


def names_divergence(path: Path) -> list[str]:
    """Every import target (any dotted part), attribute, bare name, alias or exact string in the file that IS `divergence`
    (an exact token: a future `kl_divergence` does not trip it)."""
    hits = [m for m in targets(path) if "divergence" in m.split(".")]
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.Attribute) and n.attr == "divergence":
            hits.append(n.attr)
        elif isinstance(n, ast.Name) and n.id == "divergence":
            hits.append(n.id)
        elif isinstance(n, ast.alias) and "divergence" in (n.name.split(".") + [n.asname or ""]):
            hits.append(n.name)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in ("divergence", "tret.evaluation.divergence"):
            hits.append(n.value)
    return hits


def engine_modules() -> list[Path]:
    """Every module of tret/evaluation but divergence itself: new ones are covered the day they are added."""
    mods = sorted(p for p in EVAL.glob("*.py") if p.name not in ("divergence.py", "__init__.py"))
    assert len(mods) >= 15
    return mods


DIVERGENCE_STDLIB = {"__future__", "asyncio", "hashlib", "json", "logging", "random", "uuid", "datetime", "typing"}
DIVERGENCE_ALLOWED = ("sqlalchemy", "tret.config", "tret.storage", "tret.db.models", "tret.evaluation.ratings")
DIVERGENCE_NAMES = {"tret.evaluation.juror", "tret.evaluation.juror.mask_names"}


def divergence_import_ok(mod: str) -> bool:
    top = mod.split(".")[0]
    return top in DIVERGENCE_STDLIB or mod in DIVERGENCE_NAMES or any(mod == a or mod.startswith(a + ".") for a in DIVERGENCE_ALLOWED)


def test_nothing_in_engine_1_or_2_names_divergence_and_divergence_imports_from_an_allowlist():
    for path in engine_modules():
        assert names_divergence(path) == [], path.name
        src = path.read_text()
        assert "DivergenceSet" not in src and "SpotCheck" not in src and "divergence_sets" not in src and "spot_checks" not in src, path.name
    mods = targets(EVAL / "divergence.py")
    assert mods and [m for m in mods if not divergence_import_ok(m)] == []
    # what the allowlist is there to refuse
    for bad in ("tret.evaluation.tournament", "tret.evaluation.jury", "tret.evaluation.credibility", "tret.evaluation.sealed", "tret.providers.base",
                "tret.router_llm.router", "tret.engine.routed_call", "tret.engine", "tret.evaluation.juror.build_juror_messages", "os", "httpx"):
        assert not divergence_import_ok(bad), bad
    src = (EVAL / "divergence.py").read_text()
    assert "routed_json_call" not in src and "complete_json" not in src


def test_the_divergence_lint_catches_what_it_should(tmp_path):
    f = tmp_path / "x.py"
    for src in ("from tret.evaluation import divergence", "from tret.evaluation import divergence as d", "import tret.evaluation.divergence",
                "import tret.evaluation as e\ne.divergence.build_join", "x = 'divergence'"):
        f.write_text(src)
        assert names_divergence(f), src
    for ok in ("from tret.evaluation import ratings", "kl_divergence = 1\nx.js_divergence()", "from scipy.stats import kl_divergence"):
        f.write_text(ok)
        assert names_divergence(f) == [], ok


def two_reports(world):
    return seed_reports(world, [body(1) + " ALPHAUNIQUE", body(2) + " BETAUNIQUE"])


def test_payload_contains_only_its_report_and_verifies(env):
    batch, (a, b) = two_reports(env.world)
    pa = asyncio.run(_payload(env, a))
    assert "ALPHAUNIQUE" in pa.user and "BETAUNIQUE" not in pa.user and pa.input_mode == "whole"
    pid = create(env, batch_id=batch)
    start(env, pid)
    rs = reviews(env, pid)
    assert all(r.status == "done" and r.prompt_sha256 and r.report_sha256 for r in rs)
    for c in env.judge.calls:
        assert ("ALPHAUNIQUE" in c["prompt"]) != ("BETAUNIQUE" in c["prompt"])
        assert "Company" not in c["prompt"] and "Company" not in c["system"]

    async def verify(rid):
        async with env.world.factory() as db:
            return await cr.verify_review(db, workspace_id=env.world.ws.id, review_id=rid)

    assert all(asyncio.run(verify(r.id))["ok"] for r in rs)
    target = rs[0]
    rep = fetch(env.world, lambda db: db.get(DisclosureReport, target.disclosure_report_id))
    asyncio.run(get_store().put(rep.markdown_key, b"# tampered"))
    v = asyncio.run(verify(target.id))
    assert not v["ok"] and v["reason"]


async def _payload(env, rid):
    async with env.world.factory() as db:
        return await sealed.credibility_payload(db, rid, max_input_tokens=50_000)


def test_sealed_call_writes_no_run_outcome_and_is_stamped(env):
    from tret.batches.rubrics import SEALED_CALL_KEY, is_judge_run
    from tret.db.models import Run
    batch, _ = two_reports(env.world)
    pid = create(env, batch_id=batch)
    start(env, pid)
    runs = fetch(env.world, lambda db: db.execute(select(Run))).scalars().all()
    mine = [r for r in runs if (r.task_input or {}).get(SEALED_CALL_KEY)]
    assert len(mine) == 2 and all(is_judge_run(r.task_input) and r.status == "completed" for r in mine)
    assert fetch(env.world, lambda db: db.execute(select(RunOutcome))).scalars().all() == []


MD_LONG = "# Report\n\n## Chief Executive letter\n" + "We lead. " * 50 + "\n\n## Office recycling\n" + "bin " * 6200 + \
    "\n\n## Principal risks\n" + "Supply chain exposure in one region. " * 40 + "\n\n## Governance\n" + "The board meets. " * 40 + \
    "\n\n## Capital allocation\n" + "Capex plan. " * 40 + "\n\n## Page 9\nfiller\n"


def test_digest_keeps_outline_shares_and_priority_sections_within_budget():
    d = sealed.build_digest(MD_LONG, 1500)
    assert sealed.estimate_tokens(d) <= 1500
    assert "Office recycling: 6,200 words" in d and "%)" in d
    assert "We lead." in d and "Supply chain exposure" in d
    # priority fill: with a tiny budget the letter wins, later sections are cut or absent
    small = sealed.build_digest(MD_LONG, 400)
    assert sealed.estimate_tokens(small) <= 400 and "Office recycling" in small
    tight = sealed.build_digest(MD_LONG, 700)
    assert "truncated" in tight or "Capex plan" not in tight
    assert "report_extractions" not in d


def test_whole_when_it_fits_and_digest_when_not(env):
    _, (a,) = seed_reports(env.world, [MD_LONG])[0], seed_reports(env.world, [MD_LONG])[1][:1]
    whole = asyncio.run(_payload(env, a))
    assert whole.input_mode == "whole"

    async def small():
        async with env.world.factory() as db:
            return await sealed.credibility_payload(db, a, max_input_tokens=3000)

    p = asyncio.run(small())
    assert p.input_mode == "digest" and p.est_input_tokens <= 3100 and p.report_sha256 == whole.report_sha256
    assert p.prompt_sha256 != whole.prompt_sha256


def test_clean_answer_is_strict():
    from tests.eval_credibility_support import answer
    assert sealed.clean_answer(answer(7))["credibility_score"] == 7
    for bad in ({**answer(), "credibility_score": 11}, {**answer(), "credibility_score": True}, {**answer(), "pushbacks": answer()["pushbacks"][:2]},
                {**answer(), "extra": 1}, {**answer(), "summary": "x" * 801}, {**answer(), "story_breaks": ["b"] * 7}):
        with pytest.raises(ValueError):
            sealed.clean_answer(bad)


def _words(n, w="word"):
    return " ".join(f"{w}{i % 50}" for i in range(n))


UMBRELLA = ("# Strategic report\n\n## CEO review\n" + _words(300, "ceo") + "\n\n## Office recycling\n" + _words(6000, "bin") +
            "\n\n## Principal risks\n" + _words(2500, "riskword") + "\n\n# Governance\n" + _words(2500, "govword") + "\n")

ACCOUNTING = ("# Annual report\n\n## Summary of significant accounting policies\n" + _words(4000, "acct") +
              "\n\n## Principal risks\n" + _words(2500, "riskword") + "\n\n## Governance\n" + _words(2500, "govword") + "\n")


@pytest.mark.parametrize("md,bad", [(UMBRELLA, "bin"), (ACCOUNTING, "acct")])
def test_digest_keeps_risk_and_governance_when_a_big_section_would_eat_the_budget(md, bad):
    d = sealed.build_digest(md, 3000)
    used = sealed.estimate_tokens(d)
    assert used <= 3000 and used >= 0.85 * 3000
    assert "riskword" in d and "govword" in d
    assert bad + "1 " not in d.replace("\n", " ").split("OUTLINE")[1].split("VERBATIM SECTIONS")[1] or bad == "bin"
    assert "Office recycling: 6,000 words" in d or md is ACCOUNTING


def test_digest_truncates_at_a_line_near_the_limit():
    md = "# R\n\n## Principal risks\n" + "\n".join(f"line {i} " + _words(20) for i in range(400)) + "\n"
    d = sealed.build_digest(md, 1500)
    assert sealed.estimate_tokens(d) >= 0.85 * 1500 and "truncated" in d
