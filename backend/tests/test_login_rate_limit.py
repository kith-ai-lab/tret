"""Login rate limiting: the sliding window and the endpoint that uses it."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bench.api import auth
from bench.api.auth import SlidingWindowLimiter, login_limiter
from bench.db.engine import get_db


# ── the limiter itself ───────────────────────────────────────────────────────
def test_allows_up_to_the_limit_then_blocks():
    limiter = SlidingWindowLimiter()
    for i in range(3):
        assert limiter.check("k", 3, 60.0, now=i) == 0.0
        limiter.record("k", now=i)
    assert limiter.check("k", 3, 60.0, now=3) > 0


def test_window_slides_so_old_attempts_expire():
    limiter = SlidingWindowLimiter()
    for i in range(3):
        limiter.record("k", now=i)
    assert limiter.check("k", 3, 60.0, now=59) > 0
    # The oldest hit (t=0) ages out at t=60, freeing one slot.
    assert limiter.check("k", 3, 60.0, now=60.5) == 0.0


def test_retry_after_counts_down():
    limiter = SlidingWindowLimiter()
    for i in range(2):
        limiter.record("k", now=i)
    wait = limiter.check("k", 2, 10.0, now=5)
    assert wait == pytest.approx(5.0)


def test_keys_are_independent():
    limiter = SlidingWindowLimiter()
    for i in range(3):
        limiter.record("a", now=i)
    assert limiter.check("a", 3, 60.0, now=3) > 0
    assert limiter.check("b", 3, 60.0, now=3) == 0.0


def test_reset_clears_one_key_or_all():
    limiter = SlidingWindowLimiter()
    limiter.record("a", now=0)
    limiter.record("b", now=0)
    limiter.reset("a")
    assert limiter.check("a", 1, 60.0, now=1) == 0.0
    assert limiter.check("b", 1, 60.0, now=1) > 0
    limiter.reset()
    assert limiter.check("b", 1, 60.0, now=1) == 0.0


def test_expired_keys_are_pruned():
    limiter = SlidingWindowLimiter()
    for i in range(50):
        limiter.record(f"key-{i}", now=i)
    limiter.check("key-0", 1, 10.0, now=1000)
    assert limiter._hits == {}


# ── the endpoint ─────────────────────────────────────────────────────────────
class FakeResult:
    def scalar_one_or_none(self):
        return None  # no such user -> "Invalid credentials"


class FakeSession:
    async def execute(self, *_args, **_kwargs):
        return FakeResult()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(auth.router)
    app.dependency_overrides[get_db] = lambda: FakeSession()
    login_limiter.reset()
    yield TestClient(app)
    login_limiter.reset()


def _attempt(client, email="nobody@example.com"):
    return client.post("/api/auth/login", json={"email": email, "password": "wrong-password"})


def test_repeated_failures_get_429_with_retry_after(client, monkeypatch):
    settings = auth.get_settings()
    monkeypatch.setattr(settings, "login_max_attempts", 3)
    monkeypatch.setattr(settings, "login_window_seconds", 300.0)

    for _ in range(3):
        assert _attempt(client).status_code == 401
    blocked = _attempt(client)
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    assert "Too many failed login attempts" in blocked.json()["detail"]


def test_limit_is_per_email(client, monkeypatch):
    monkeypatch.setattr(auth.get_settings(), "login_max_attempts", 2)
    for _ in range(2):
        assert _attempt(client, "a@example.com").status_code == 401
    assert _attempt(client, "a@example.com").status_code == 429
    assert _attempt(client, "b@example.com").status_code == 401  # different key


def test_email_case_and_whitespace_share_one_bucket(client, monkeypatch):
    monkeypatch.setattr(auth.get_settings(), "login_max_attempts", 2)
    assert _attempt(client, "a@example.com").status_code == 401
    assert _attempt(client, " A@Example.com ").status_code == 401
    assert _attempt(client, "a@example.com").status_code == 429
