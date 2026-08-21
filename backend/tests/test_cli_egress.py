"""`tret egress` at the command line — status, cut, restore.

The property worth defending here is not the table formatting, it's the
honesty requirement: `cut`/`restore` narrow `tret.net.policy`'s in-memory
runtime override, which is *process-local* state. A CLI invocation is its own
short-lived process, so a `cut` here never touches a running server — and the
whole point of shipping this command is that it says so, loudly, every time,
rather than looking like a working kill switch it isn't.

Runtime overrides are module-global (`tret.net.policy._runtime_overrides`),
so a leftover one would corrupt whichever egress test runs next in this
process — hence the autouse fixture, mirroring `tests/test_egress_policy.py`.
"""
from __future__ import annotations

import pytest

from tret.cli import main
from tret.config import get_settings
from tret.net import policy
from tret.net.policy import EGRESS_CLASSES


@pytest.fixture(autouse=True)
def _no_leaked_overrides():
    policy.clear_all_runtime_overrides()
    yield
    policy.clear_all_runtime_overrides()


def _run(monkeypatch, *argv: str) -> None:
    """Drive `tret.cli.main()` the way a real invocation would: through argv,
    not by calling the handler function directly, so the test also exercises
    argparse's dispatch and `choices=` validation."""
    monkeypatch.setattr("sys.argv", ["tret", *argv])
    main()


def _mode_of(output: str, egress_class: str) -> str:
    """Pull the MODE column out of the class's table row. MODE is always the
    second whitespace-separated token, regardless of how HOSTS (which can
    itself contain spaces, after the comma-joins) pads out the line."""
    (line,) = (row for row in output.splitlines() if row.startswith(egress_class))
    return line.split()[1]


def test_status_prints_every_class_and_the_master_line(monkeypatch, capsys):
    _run(monkeypatch, "egress", "status")
    out = capsys.readouterr().out
    assert "master:" in out
    for name in EGRESS_CLASSES:
        assert name in out


def test_cut_narrows_even_though_the_environment_says_on(monkeypatch, capsys):
    """The override beats the environment (narrower always wins): `status`
    must read research off after `cut`, even with TRET_EGRESS_RESEARCH=on."""
    monkeypatch.setenv("TRET_EGRESS_RESEARCH", "on")
    get_settings.cache_clear()
    try:
        _run(monkeypatch, "egress", "cut", "research")
        capsys.readouterr()  # discard cut's own printed line + warning
        _run(monkeypatch, "egress", "status")
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert _mode_of(out, "research") == "off"


def test_restore_returns_to_what_the_environment_says(monkeypatch, capsys):
    monkeypatch.setenv("TRET_EGRESS_RESEARCH", "on")
    get_settings.cache_clear()
    try:
        _run(monkeypatch, "egress", "cut", "research")
        capsys.readouterr()
        _run(monkeypatch, "egress", "restore", "research")
        capsys.readouterr()
        _run(monkeypatch, "egress", "status")
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert _mode_of(out, "research") == "on"


def test_an_unknown_class_is_refused_before_any_handler_runs(monkeypatch):
    """`egress_class` is declared with argparse `choices=EGRESS_CLASSES`, so a
    bad class never reaches `set_runtime_override` — argparse rejects it and
    exits 2, the standard argparse usage-error code, not a stack trace."""
    monkeypatch.setattr("sys.argv", ["tret", "egress", "cut", "not-a-class"])
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 2


def test_cut_warns_that_the_effect_is_process_local(monkeypatch, capsys):
    """The honesty requirement: a `cut` from the CLI must not read as having
    touched a running server. This is the phrase that has to survive, not the
    exact wording, since the wording is free to change."""
    _run(monkeypatch, "egress", "cut", "research")
    err = capsys.readouterr().err
    assert "process's memory only" in err
    assert "does NOT reach a running server" in err
