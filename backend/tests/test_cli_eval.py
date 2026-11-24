"""`tret eval jury-preview`: argument errors and the readable rendering (no database, no model)."""
from __future__ import annotations

import argparse

from tret import cli_eval
from tret.router_llm.jury_select import JuryPick, JurySelection


def test_render_shows_each_pick_with_its_reason():
    sel = JurySelection(
        purpose="eval_jury", k=1, distinct_authors=True, policy={"mode": "auto"}, objective="balanced",
        open_weights="any", min_context_window=0, task_shape="extraction", task_family=None,
        ranking_version="jury-v1", router_version="r", priors_version="p", exploration="off", llm_call=False,
        eligible=3, families_eligible=2,
        picks=[JuryPick("m/a", "openai", 1, {}, "Ranked 1 of 3.", [{"model": "m/b", "rank": 2}])],
        skipped_families=[{"family": "google", "best_model": "m/g", "best_rank": 3, "reason": "x"}],
        excluded_models=[], created_at="now",
    )
    text = cli_eval.render(sel.to_json())
    assert "1. m/a  [family openai, rank 1]" in text and "Ranked 1 of 3." in text
    assert "m/b (rank 2)" in text and "google" in text and "no model was called" in text


def test_bad_arguments_exit_2(capsys):
    base = dict(k=3, policy_json=None, min_context=0, allow_repeat_families=False, purpose="eval_jury", workspace=None, json=False)
    assert cli_eval.run(argparse.Namespace(**{**base, "policy_json": "[1]"})) == 2
    assert cli_eval.run(argparse.Namespace(**{**base, "policy_json": "{nope"})) == 2
    assert cli_eval.run(argparse.Namespace(**{**base, "workspace": "zzz"})) == 2
    assert "error:" in capsys.readouterr().err
