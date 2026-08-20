"""Login rate limiting: the sliding window and the endpoint that uses it."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tret.api import auth
from tret.api.auth import SlidingWindowLimiter, login_limiter
from tret.db.engine import get_db


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


# ── the keys, and what survives a reverse proxy ──────────────────────────────
# docs/hardening.md requires a reverse proxy in front of tret, which makes
# `request.client.host` the proxy for every request. A limiter keyed only on that
# would silently become one bucket for the whole internet, and keying on
# X-Forwarded-For instead would hand the attacker a knob for minting fresh
# buckets. These tests pin the resolution: two buckets, both keyed on the account,
# neither influenced by a client-settable header.
class FakeRequest:
    def __init__(self, host: str | None, headers: dict | None = None):
        self.client = None if host is None else type("Peer", (), {"host": host})()
        self.headers = headers or {}


class FakeSettings:
    def __init__(self, max_attempts: int):
        self.login_max_attempts = max_attempts


def _keys(host, email, max_attempts=10):
    return auth._login_buckets(FakeRequest(host), email, FakeSettings(max_attempts))


def test_both_buckets_are_keyed_on_the_account():
    buckets = _keys("10.0.0.1", "Victim@Example.com ", max_attempts=4)
    assert buckets == [
        ("src|10.0.0.1|victim@example.com", 4),
        ("acct|victim@example.com", 4 * auth.ACCOUNT_BURST_MULTIPLE),
    ]


def test_the_account_bucket_is_the_one_a_proxy_cannot_flatten():
    """Same peer for every request (the proxy case): the account bucket is identical."""
    behind_proxy = [_keys("172.16.0.9", "victim@example.com")[1] for _ in range(3)]
    direct = _keys("203.0.113.7", "victim@example.com")[1]
    assert set(behind_proxy) == {direct}


def test_a_missing_client_still_produces_a_key():
    assert _keys(None, "victim@example.com")[0][0] == "src|unknown|victim@example.com"


def test_a_forged_forwarded_header_cannot_mint_a_fresh_bucket(client, monkeypatch):
    """The header is not read at all, so varying it changes nothing.

    This is the property that makes ignoring X-Forwarded-For safe rather than
    lazy: an attacker who could influence the key would get a full allowance per
    made-up hop, which is strictly worse than an IP component that is constant.
    """
    monkeypatch.setattr(auth.get_settings(), "login_max_attempts", 2)
    for i in range(2):
        response = client.post(
            "/api/auth/login",
            json={"email": "victim@example.com", "password": "wrong"},
            headers={"X-Forwarded-For": f"10.0.0.{i}", "X-Real-IP": f"10.0.0.{i}"},
        )
        assert response.status_code == 401
    blocked = client.post(
        "/api/auth/login",
        json={"email": "victim@example.com", "password": "wrong"},
        headers={"X-Forwarded-For": "10.0.0.99", "Forwarded": "for=10.0.0.99"},
    )
    assert blocked.status_code == 429


def _client_from(host: str) -> TestClient:
    app = FastAPI()
    app.include_router(auth.router)
    app.dependency_overrides[get_db] = lambda: FakeSession()
    return TestClient(app, client=(host, 40000))


def test_distinct_source_hosts_keep_their_own_allowance(monkeypatch):
    """Directly exposed, one noisy host must not spend another host's budget."""
    monkeypatch.setattr(auth.get_settings(), "login_max_attempts", 2)
    login_limiter.reset()
    first, second = _client_from("198.51.100.1"), _client_from("198.51.100.2")
    for _ in range(2):
        assert _attempt(first, "victim@example.com").status_code == 401
    assert _attempt(first, "victim@example.com").status_code == 429
    assert _attempt(second, "victim@example.com").status_code == 401
    login_limiter.reset()


def test_the_account_bucket_caps_a_distributed_attempt(monkeypatch):
    """A new source address per host used to mean an unlimited total.

    With `login_max_attempts` 2 and a 5x account multiple, ten failures against
    one account exhaust the account bucket however they are spread, so the
    eleventh is refused even though its source has never been seen before.
    """
    monkeypatch.setattr(auth.get_settings(), "login_max_attempts", 2)
    login_limiter.reset()
    for host in range(5):  # 5 hosts x 2 attempts = the account allowance
        attacker = _client_from(f"203.0.113.{host}")
        for _ in range(2):
            assert _attempt(attacker, "victim@example.com").status_code == 401

    fresh_host = _client_from("203.0.113.200")
    assert _attempt(fresh_host, "victim@example.com").status_code == 429
    # A different account is untouched: a lockout is never deployment-wide.
    assert _attempt(fresh_host, "someone-else@example.com").status_code == 401
    login_limiter.reset()
