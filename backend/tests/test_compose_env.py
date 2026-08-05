"""docker-compose.yml, .env.example and Settings must agree.

The documented quickstart is `cp .env.example .env && docker compose up`, and
.env.example presents itself as live configuration. But compose reads .env only
to interpolate `${...}` references: nothing copies it into the container and the
backend image has no .env of its own. So a knob documented in .env.example that
is not referenced in the backend service's `environment:` block is silently
ignored — the operator sets their grid intensity, the deploy reports world-
average carbon, and nothing anywhere says otherwise.

These tests are the CI guard against that drift in both directions: every
documented name reaches the container, and the defaults written into compose are
the same defaults the code has.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from bench.config import Settings

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker-compose.yml"
ENV_EXAMPLE = REPO / ".env.example"

# Names that legitimately do not belong in the backend service environment.
NOT_BACKEND_SETTINGS = {
    # Consumed by the ollama-init service, not by the backend.
    "BENCH_LOCAL_PULL_MODEL",
}

# Values compose deliberately fixes to container paths / service names, so they
# must NOT match the code default (which is tuned for a bare local checkout).
CONTAINER_OVERRIDES = {
    "BENCH_DATABASE_URL",
    "BENCH_STORAGE_DIR",
    "BENCH_PACKS_DIR",
}


def _backend_environment() -> dict[str, str]:
    compose = yaml.safe_load(COMPOSE.read_text())
    env = compose["services"]["backend"]["environment"]
    if isinstance(env, list):  # compose also allows a "KEY=value" list
        return dict(item.split("=", 1) for item in env)
    return {k: "" if v is None else str(v) for k, v in env.items()}


def _documented_names() -> set[str]:
    """Every BENCH_* name .env.example mentions, commented examples included —
    a commented knob is still documented as working."""
    return set(re.findall(r"\bBENCH_[A-Z0-9_]+", ENV_EXAMPLE.read_text()))


def _settings_env_names() -> set[str]:
    return {f"BENCH_{name.upper()}" for name in Settings.model_fields}


def test_every_documented_env_var_reaches_the_backend_container():
    missing = sorted(
        name
        for name in _documented_names() - NOT_BACKEND_SETTINGS
        if name not in _backend_environment()
    )
    assert not missing, (
        "documented in .env.example but never passed to the backend service, so "
        f"setting it does nothing: {missing}"
    )


def test_every_backend_env_var_is_a_real_setting():
    """The other direction: no compose entry the code does not read."""
    unknown = sorted(set(_backend_environment()) - _settings_env_names())
    assert not unknown, f"passed to the backend but not a Settings field: {unknown}"


def test_every_setting_is_either_wired_or_deliberately_absent():
    """A new Settings field should be reachable from the quickstart, or
    explicitly not (frontend-serving is a single-app/Fly concern)."""
    deliberately_absent = {"BENCH_SERVE_FRONTEND_DIR"}
    absent = _settings_env_names() - set(_backend_environment()) - deliberately_absent
    assert not absent, f"Settings fields unreachable under docker compose: {sorted(absent)}"


@pytest.mark.parametrize("name", sorted(_settings_env_names() & set(_backend_environment())))
def test_compose_defaults_match_the_code_defaults(name):
    """`${VAR:-default}` duplicates a default, so CI checks the copies agree."""
    if name in CONTAINER_OVERRIDES:
        pytest.skip("deliberately fixed to a container path or service name")
    value = _backend_environment()[name]
    match = re.fullmatch(r"\$\{%s(?::-(.*))?\}" % name, value)
    assert match, f"{name} should be written as ${{{name}:-<default>}}, found {value!r}"

    compose_default = match.group(1) or ""
    code_default = Settings.model_fields[name[len("BENCH_") :].lower()].default
    expected = "" if code_default is None else str(code_default)
    # Booleans read as true/false in YAML-land, not Python's True/False.
    if isinstance(code_default, bool):
        expected = str(code_default).lower()
    assert compose_default == expected, (
        f"{name}: compose default {compose_default!r} != code default {expected!r}"
    )


def test_blank_optional_float_is_read_as_unset():
    """What makes `${BENCH_LOCAL_GRID_CO2E_G_PER_KWH:-}` safe: an empty value is
    "not set", not a parse error — and not a hardcoded number that would defeat
    the documented fallback to BENCH_GRID_CO2E_G_PER_KWH."""
    assert Settings(local_grid_co2e_g_per_kwh="").local_grid_co2e_g_per_kwh is None
    assert Settings(local_grid_co2e_g_per_kwh="  ").local_grid_co2e_g_per_kwh is None
    assert Settings(local_grid_co2e_g_per_kwh="30").local_grid_co2e_g_per_kwh == 30.0


def test_every_numeric_or_bool_default_in_compose_parses():
    """The empty-string trap: compose passing "" for an int/float/bool knob would
    stop the backend booting. Every default written above must be parseable."""
    env = _backend_environment()
    overrides = {}
    for name, value in env.items():
        match = re.fullmatch(r"\$\{%s(?::-(.*))?\}" % name, value)
        overrides[name[len("BENCH_") :].lower()] = match.group(1) or "" if match else value
    Settings(**overrides)  # must not raise
