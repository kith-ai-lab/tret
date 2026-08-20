"""Production boot checks: refuse to start on the shipped development defaults."""
import pytest

from tret.config import (
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_SECRET_KEY,
    InsecureConfigError,
    Settings,
    enforce_production_safety,
    is_production,
    production_config_problems,
)


def _settings(**over) -> Settings:
    base = {
        "environment": "production",
        "secret_key": "a-real-random-secret",
        "admin_password": "a-real-admin-password",
        "cookie_secure": True,
    }
    base.update(over)
    return Settings(**base)


def test_shipped_defaults_are_the_checked_constants():
    assert Settings.model_fields["secret_key"].default == DEFAULT_SECRET_KEY
    assert Settings.model_fields["admin_password"].default == DEFAULT_ADMIN_PASSWORD
    assert Settings.model_fields["environment"].default == "development"


def test_development_is_never_blocked():
    dev = Settings(
        environment="development",
        secret_key=DEFAULT_SECRET_KEY,
        admin_password=DEFAULT_ADMIN_PASSWORD,
        cookie_secure=False,
    )
    assert production_config_problems(dev) == ([], [])
    enforce_production_safety(dev)  # does not raise


def test_production_is_detected_case_insensitively():
    assert is_production(Settings(environment="Production"))
    assert is_production(Settings(environment="prod"))
    assert not is_production(Settings(environment="staging"))


def test_hardened_production_config_passes():
    assert production_config_problems(_settings()) == ([], [])
    enforce_production_safety(_settings())


def test_default_secret_key_is_fatal_in_production():
    fatal, _warnings = production_config_problems(_settings(secret_key=DEFAULT_SECRET_KEY))
    assert len(fatal) == 1
    assert "TRET_SECRET_KEY" in fatal[0]
    with pytest.raises(InsecureConfigError, match="TRET_SECRET_KEY"):
        enforce_production_safety(_settings(secret_key=DEFAULT_SECRET_KEY))


def test_empty_secret_key_is_fatal_in_production():
    fatal, _ = production_config_problems(_settings(secret_key="   "))
    assert len(fatal) == 1


def test_default_admin_password_is_fatal_in_production():
    fatal, _ = production_config_problems(_settings(admin_password=DEFAULT_ADMIN_PASSWORD))
    assert len(fatal) == 1
    assert "TRET_ADMIN_PASSWORD" in fatal[0]
    with pytest.raises(InsecureConfigError, match="TRET_ADMIN_PASSWORD"):
        enforce_production_safety(_settings(admin_password=DEFAULT_ADMIN_PASSWORD))


def test_both_defaults_are_reported_together():
    fatal, _ = production_config_problems(
        _settings(secret_key=DEFAULT_SECRET_KEY, admin_password=DEFAULT_ADMIN_PASSWORD)
    )
    assert len(fatal) == 2


def test_insecure_cookie_warns_but_does_not_block():
    fatal, warnings = production_config_problems(_settings(cookie_secure=False))
    assert fatal == []
    assert len(warnings) == 1
    assert "TRET_COOKIE_SECURE" in warnings[0]

    class Recorder:
        def __init__(self):
            self.messages = []

        def warning(self, fmt, *args):
            self.messages.append(fmt % args)

    recorder = Recorder()
    enforce_production_safety(_settings(cookie_secure=False), log=recorder)  # no raise
    assert any("TRET_COOKIE_SECURE" in m for m in recorder.messages)
