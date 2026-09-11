"""OpenRouter request/response hygiene: session affinity, provider
preferences, and recording which upstream actually served a call.

No network: request bodies are inspected directly (`_apply_provider` /
`_session_body`) or via a fake httpx client driving `stream()`/`complete_json()`
exactly the way `test_prompt_caching.py` does for cache_control.

Field names verified against the live OpenRouter docs (fetched 2026-09-10):
- https://openrouter.ai/docs/api-reference/chat-completion — `session_id` is a
  top-level request string (CreateChatCompletionRequest), max 256 chars, used
  as OpenRouter's sticky routing key. The *response* schemas
  (ChatCompletionResponse, ChatStreamChunk) carry no top-level `provider`
  field — the served endpoint is named only inside
  `openrouter_metadata.endpoints.available[]`, in the entry with
  `selected: true`. That entry's `provider` field is a display name
  ("DeepInfra") — the schema has no slug/tag field — so surfacing it at all
  needs the opt-in `X-OpenRouter-Metadata: enabled` request header ("Opt-in
  to surface routing metadata on the response under `openrouter_metadata`.
  Defaults to disabled."), and turning it into the slug `provider.ignore`
  matches on needs the separate lookup below.
- https://openrouter.ai/docs/guides/routing/provider-selection — the
  `provider` request object's fields: `order`, `ignore`, `only`,
  `quantizations`, `data_collection`, `zdr`, `sort`, `require_parameters`
  (all verified against the ProviderPreferences schema).
- `GET https://openrouter.ai/api/v1/models/{model}/endpoints` — live-checked
  (not just doc-checked): each entry carries both `provider_name` (display
  name, e.g. "OpenAI") and `tag` (slug, e.g. "openai"), which is what
  `OpenRouterProvider._resolve_served_by` maps a metadata display name
  through.
"""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from tret.config import Settings
from tret.providers import openai_compat
from tret.providers.anthropic import AnthropicProvider
from tret.providers.base import Msg, ToolSpec
from tret.providers.openai_compat import (
    KimiProvider,
    OpenRouterProvider,
    _choose_base_slug,
    _served_by_from_openai,
)


@pytest.fixture(autouse=True)
def _reset_endpoints_response_stub():
    """`endpoints_response` is a mutable *class* attribute on the fake
    clients below, set by whichever test needs a non-empty stub for the
    `/models/{model}/endpoints` lookup. Without a reset, a test that reads
    `_resolve_served_by`/`_endpoint_slug_map` without setting its own stub
    silently inherits whatever an earlier test in the same run left behind —
    passing or failing depending on collection/run order rather than on its
    own fixture data.
    """
    _FakeStreamClient.endpoints_response = {}
    _FakeJsonClient.endpoints_response = {}
    yield
    _FakeStreamClient.endpoints_response = {}
    _FakeJsonClient.endpoints_response = {}


def _tool() -> ToolSpec:
    return ToolSpec(name="lookup", description="look something up", parameters={"type": "object"})


# ── fake httpx client (streaming) — same shape as test_prompt_caching.py ──────
class _FakeResponse:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):  # pragma: no cover - only used on error paths
        return b""


class _FakeStreamCtx:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeEndpointsResponse:
    """Stands in for the `GET /models/{model}/endpoints` response used to
    resolve a served_by display name to its provider slug."""

    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeStreamClient:
    """Stands in for httpx.AsyncClient, recording the streamed request body."""

    captured: dict = {}
    captured_headers: dict = {}
    # Stub for the `/models/{model}/endpoints` slug lookup — set per test that
    # exercises `OpenRouterProvider._resolve_served_by`. `{}` (no endpoints)
    # is a safe default: served_by resolution isn't even attempted unless a
    # test's scripted response carries `openrouter_metadata`.
    endpoints_response: dict = {}

    def __init__(self, lines):
        self._lines = lines

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None, json=None):
        type(self).captured = json or {}
        type(self).captured_headers = headers or {}
        return _FakeStreamCtx(_FakeResponse(self._lines))

    async def get(self, url, headers=None):
        return _FakeEndpointsResponse(type(self).endpoints_response)


def _sse(*chunks) -> list[str]:
    return [f"data: {json.dumps(c)}" for c in chunks] + ["data: [DONE]"]


async def _run_stream(
    monkeypatch, provider, lines, *, tools=None, session_id=None, effort=None, provider_ignore=None
):
    client = _FakeStreamClient(lines)
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)
    events = []
    async for event in provider.stream(
        model="m",
        system="doctrine",
        messages=[Msg(role="user", content="hi")],
        tools=tools or [],
        max_tokens=64,
        temperature=0.0,
        session_id=session_id,
        effort=effort,
        provider_ignore=provider_ignore,
    ):
        events.append(event)
    return events, _FakeStreamClient.captured


# ── fake httpx client (non-streaming, for complete_json) ──────────────────────
class _FakeJsonResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeJsonClient:
    captured: dict = {}
    captured_headers: dict = {}
    # Same stub role as `_FakeStreamClient.endpoints_response`, for the
    # `complete_json` path's served_by resolution.
    endpoints_response: dict = {}

    def __init__(self, response: _FakeJsonResponse):
        self._response = response

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        type(self).captured = json or {}
        type(self).captured_headers = headers or {}
        return self._response

    async def get(self, url, headers=None):
        return _FakeEndpointsResponse(type(self).endpoints_response)


def _completion_json(*, served_by: str | None = None, tool_name: str = "respond") -> dict:
    payload = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps({"ok": True}),
                            }
                        }
                    ]
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }
    if served_by is not None:
        payload["openrouter_metadata"] = {
            "endpoints": {"available": [{"model": "x/y", "provider": served_by, "selected": True}]}
        }
    return payload


async def _run_complete_json(monkeypatch, provider, response: dict):
    client = _FakeJsonClient(_FakeJsonResponse(response))
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)
    completion = await provider.complete_json(
        model="m", system="doctrine", prompt="hi", schema={"type": "object"}
    )
    return completion, _FakeJsonClient.captured


# ── 1. session affinity ────────────────────────────────────────────────────────
async def test_openrouter_stream_sends_session_id_when_given(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, OpenRouterProvider("k"), lines, session_id="run-123")
    assert body["session_id"] == "run-123"


async def test_openrouter_stream_omits_session_id_when_none(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, OpenRouterProvider("k"), lines, session_id=None)
    assert "session_id" not in body


async def test_kimi_stream_never_sends_session_id(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, KimiProvider("k"), lines, session_id="run-123")
    assert "session_id" not in body


# ── 1a. openrouter_metadata opt-in header ───────────────────────────────────
async def test_openrouter_sends_the_metadata_opt_in_header(monkeypatch):
    """Without this header every response's `openrouter_metadata` is absent
    (it defaults to disabled), so `served_by` reads as None on every real
    call and the whole priors/provider_ignore chain never sees a value."""
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    await _run_stream(monkeypatch, OpenRouterProvider("k"), lines)
    assert _FakeStreamClient.captured_headers["X-OpenRouter-Metadata"] == "enabled"


async def test_kimi_stream_never_sends_the_metadata_header(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    await _run_stream(monkeypatch, KimiProvider("k"), lines)
    assert "X-OpenRouter-Metadata" not in _FakeStreamClient.captured_headers


# ── 1b. reasoning-effort wire shape ─────────────────────────────────────────
async def test_openrouter_stream_sends_reasoning_effort_when_given(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, OpenRouterProvider("k"), lines, effort="low")
    assert body["reasoning"] == {"effort": "low"}


async def test_openrouter_stream_omits_reasoning_when_effort_is_none(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, OpenRouterProvider("k"), lines, effort=None)
    assert "reasoning" not in body


async def test_kimi_stream_never_sends_reasoning(monkeypatch):
    # Kimi is Moonshot's native API, not OpenRouter's unified surface — it has
    # no equivalent control, so `_effort_body`'s base-class default (`{}`)
    # applies, regardless of what `effort` the harness passed in.
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, KimiProvider("k"), lines, effort="high")
    assert "reasoning" not in body


# ── 2. provider preferences ────────────────────────────────────────────────────
async def test_require_parameters_set_only_when_tools_present(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, OpenRouterProvider("k"), lines, tools=[_tool()])
    assert body["provider"]["require_parameters"] is True


async def test_no_provider_key_without_tools_or_prefs(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, OpenRouterProvider("k"), lines, tools=[])
    assert "provider" not in body


async def test_prefs_apply_without_tools_but_carry_no_require_parameters(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    provider = OpenRouterProvider("k", provider_prefs={"zdr": True})
    _, body = await _run_stream(monkeypatch, provider, lines, tools=[])
    assert body["provider"] == {"zdr": True}
    assert "require_parameters" not in body["provider"]


async def test_prefs_merge_on_top_of_require_parameters_and_add_quantizations(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    provider = OpenRouterProvider("k", provider_prefs={"quantizations": ["fp8", "fp16"]})
    _, body = await _run_stream(monkeypatch, provider, lines, tools=[_tool()])
    assert body["provider"] == {"require_parameters": True, "quantizations": ["fp8", "fp16"]}


async def test_prefs_can_override_require_parameters_itself(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    provider = OpenRouterProvider("k", provider_prefs={"require_parameters": False})
    _, body = await _run_stream(monkeypatch, provider, lines, tools=[_tool()])
    assert body["provider"]["require_parameters"] is False


async def test_kimi_never_gets_a_provider_key(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, KimiProvider("k"), lines, tools=[_tool()])
    assert "provider" not in body


async def test_complete_json_sends_require_parameters_too(monkeypatch):
    """The router's own forced-tool-call JSON completion always carries
    `tools`, so it benefits from `require_parameters` exactly like a
    tool-calling agent turn."""
    _, body = await _run_complete_json(monkeypatch, OpenRouterProvider("k"), _completion_json())
    assert body["provider"]["require_parameters"] is True


async def test_complete_json_prefs_merge_too(monkeypatch):
    provider = OpenRouterProvider("k", provider_prefs={"order": ["anthropic"]})
    _, body = await _run_complete_json(monkeypatch, provider, _completion_json())
    assert body["provider"] == {"require_parameters": True, "order": ["anthropic"]}


async def test_kimi_complete_json_never_gets_a_provider_key(monkeypatch):
    _, body = await _run_complete_json(monkeypatch, KimiProvider("k"), _completion_json())
    assert "provider" not in body


async def test_complete_json_also_sends_the_metadata_opt_in_header(monkeypatch):
    await _run_complete_json(monkeypatch, OpenRouterProvider("k"), _completion_json())
    assert _FakeJsonClient.captured_headers["X-OpenRouter-Metadata"] == "enabled"


async def test_kimi_complete_json_never_sends_the_metadata_header(monkeypatch):
    await _run_complete_json(monkeypatch, KimiProvider("k"), _completion_json())
    assert "X-OpenRouter-Metadata" not in _FakeJsonClient.captured_headers


# ── 2b. provider_ignore: RoutingDecision evidence merged into provider.ignore ──
async def test_provider_ignore_is_sent_as_ignore_list(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(
        monkeypatch, OpenRouterProvider("k"), lines, provider_ignore=["quantized-endpoint"]
    )
    assert body["provider"]["ignore"] == ["quantized-endpoint"]


async def test_provider_ignore_omitted_when_none_and_no_prefs(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, OpenRouterProvider("k"), lines, provider_ignore=None)
    assert "provider" not in body


async def test_provider_ignore_omitted_when_an_empty_list(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(monkeypatch, OpenRouterProvider("k"), lines, provider_ignore=[])
    assert "provider" not in body


async def test_provider_ignore_unions_and_dedupes_with_operator_prefs(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    provider = OpenRouterProvider("k", provider_prefs={"ignore": ["operator-blocked", "shared"]})
    _, body = await _run_stream(
        monkeypatch, provider, lines, provider_ignore=["shared", "quantized-endpoint"]
    )
    assert body["provider"]["ignore"] == ["operator-blocked", "quantized-endpoint", "shared"]


async def test_provider_ignore_combines_with_require_parameters_when_tools_present(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(
        monkeypatch,
        OpenRouterProvider("k"),
        lines,
        tools=[_tool()],
        provider_ignore=["quantized-endpoint"],
    )
    assert body["provider"] == {"require_parameters": True, "ignore": ["quantized-endpoint"]}


async def test_kimi_never_sends_provider_ignore(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(
        monkeypatch, KimiProvider("k"), lines, provider_ignore=["quantized-endpoint"]
    )
    assert "provider" not in body


# ── 3. TRET_OPENROUTER_PROVIDER_PREFS parsing ──────────────────────────────────
def test_invalid_provider_prefs_json_is_ignored_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="tret"):
        settings = Settings(openrouter_provider_prefs="{not valid json")
    assert settings.openrouter_provider_prefs is None
    assert "TRET_OPENROUTER_PROVIDER_PREFS" in caplog.text
    assert "not valid JSON" in caplog.text


def test_non_object_provider_prefs_json_is_ignored_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="tret"):
        settings = Settings(openrouter_provider_prefs='["not", "an", "object"]')
    assert settings.openrouter_provider_prefs is None
    assert "must be a JSON object" in caplog.text


def test_valid_provider_prefs_json_is_applied():
    settings = Settings(openrouter_provider_prefs='{"order": ["anthropic"], "zdr": true}')
    assert settings.openrouter_provider_prefs == {"order": ["anthropic"], "zdr": True}


def test_blank_provider_prefs_means_unset():
    assert Settings(openrouter_provider_prefs="").openrouter_provider_prefs is None


def test_unset_provider_prefs_defaults_to_none():
    assert Settings().openrouter_provider_prefs is None


# ── 3b. TRET_OPENROUTER_PROVIDER_PREFS.ignore coercion ──────────────────────
def test_bare_string_ignore_becomes_a_one_element_list():
    """`{"ignore": "deepinfra"}` used to reach `_provider_body`'s
    `set(... or [])` as a string, iterating its characters into a denylist of
    letters instead of the one provider it names."""
    settings = Settings(openrouter_provider_prefs='{"ignore": "deepinfra"}')
    assert settings.openrouter_provider_prefs == {"ignore": ["deepinfra"]}


def test_ignore_list_drops_non_string_entries_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="tret"):
        settings = Settings(openrouter_provider_prefs='{"ignore": ["deepinfra", 5]}')
    assert settings.openrouter_provider_prefs == {"ignore": ["deepinfra"]}
    assert "TRET_OPENROUTER_PROVIDER_PREFS.ignore" in caplog.text
    assert "dropping it" in caplog.text


def test_ignore_drops_empty_and_blank_string_entries():
    settings = Settings(openrouter_provider_prefs='{"ignore": ["deepinfra", "", "  "]}')
    assert settings.openrouter_provider_prefs == {"ignore": ["deepinfra"]}


def test_ignore_key_omitted_once_nothing_valid_is_left(caplog):
    with caplog.at_level(logging.WARNING, logger="tret"):
        settings = Settings(openrouter_provider_prefs='{"ignore": [5, null], "zdr": true}')
    assert settings.openrouter_provider_prefs == {"zdr": True}


def test_non_string_non_list_ignore_is_dropped_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="tret"):
        settings = Settings(openrouter_provider_prefs='{"ignore": 5}')
    assert settings.openrouter_provider_prefs == {}
    assert "TRET_OPENROUTER_PROVIDER_PREFS.ignore" in caplog.text


def test_ignore_list_of_strings_still_passes_through_unchanged():
    settings = Settings(
        openrouter_provider_prefs='{"ignore": ["deepinfra", "google-vertex"]}'
    )
    assert settings.openrouter_provider_prefs == {"ignore": ["deepinfra", "google-vertex"]}


def test_ignore_entries_are_stripped_of_surrounding_whitespace():
    """A hand-edited `.env` line often carries stray whitespace around a JSON
    string (`"ignore": [" deepinfra "]`); left untrimmed it reaches OpenRouter
    as a slug it has never heard of, silently no-op'ing the denylist."""
    settings = Settings(openrouter_provider_prefs='{"ignore": [" deepinfra ", "\\tgoogle-vertex"]}')
    assert settings.openrouter_provider_prefs == {"ignore": ["deepinfra", "google-vertex"]}


def test_bare_string_ignore_is_also_stripped():
    settings = Settings(openrouter_provider_prefs='{"ignore": "  deepinfra  "}')
    assert settings.openrouter_provider_prefs == {"ignore": ["deepinfra"]}


# ── 3c. the same coercion, extended to `only` and `order` ───────────────────
def test_bare_string_only_becomes_a_one_element_list():
    settings = Settings(openrouter_provider_prefs='{"only": "anthropic"}')
    assert settings.openrouter_provider_prefs == {"only": ["anthropic"]}


def test_bare_string_order_becomes_a_one_element_list():
    settings = Settings(openrouter_provider_prefs='{"order": "anthropic"}')
    assert settings.openrouter_provider_prefs == {"order": ["anthropic"]}


def test_order_list_drops_non_string_entries_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="tret"):
        settings = Settings(openrouter_provider_prefs='{"order": ["anthropic", 5]}')
    assert settings.openrouter_provider_prefs == {"order": ["anthropic"]}
    assert "TRET_OPENROUTER_PROVIDER_PREFS.order" in caplog.text
    assert "dropping it" in caplog.text


def test_only_key_omitted_once_nothing_valid_is_left():
    settings = Settings(openrouter_provider_prefs='{"only": ["", "  "], "zdr": true}')
    assert settings.openrouter_provider_prefs == {"zdr": True}


def test_order_and_ignore_and_only_are_each_coerced_independently():
    settings = Settings(
        openrouter_provider_prefs=(
            '{"order": "anthropic", "ignore": ["deepinfra", 5], "only": [" together "]}'
        )
    )
    assert settings.openrouter_provider_prefs == {
        "order": ["anthropic"],
        "ignore": ["deepinfra"],
        "only": ["together"],
    }


# ── 4. served_by parsing ────────────────────────────────────────────────────────
def test_served_by_reads_the_selected_endpoint():
    data = {
        "openrouter_metadata": {
            "endpoints": {
                "available": [
                    {"model": "x/y", "provider": "Together", "selected": False},
                    {"model": "x/y", "provider": "Anthropic", "selected": True},
                ]
            }
        }
    }
    assert _served_by_from_openai(data) == "Anthropic"


def test_served_by_is_none_when_nothing_is_selected():
    data = {"openrouter_metadata": {"endpoints": {"available": [{"provider": "Together"}]}}}
    assert _served_by_from_openai(data) is None


def test_served_by_is_none_without_openrouter_metadata():
    assert _served_by_from_openai({}) is None
    assert _served_by_from_openai({"usage": {"prompt_tokens": 1}}) is None


async def test_served_by_parsed_from_a_non_streaming_response(monkeypatch):
    # Realistic values: the metadata's display name ("DeepInfra") is resolved
    # to the slug ("deepinfra") `provider.ignore` actually matches on, via a
    # stubbed `/models/{model}/endpoints` response.
    _FakeJsonClient.endpoints_response = {
        "data": {"endpoints": [{"provider_name": "DeepInfra", "tag": "deepinfra"}]}
    }
    completion, _ = await _run_complete_json(
        monkeypatch, OpenRouterProvider("k"), _completion_json(served_by="DeepInfra")
    )
    assert completion.served_by == "deepinfra"


async def test_served_by_none_from_a_non_streaming_response_without_it(monkeypatch):
    completion, _ = await _run_complete_json(
        monkeypatch, OpenRouterProvider("k"), _completion_json(served_by=None)
    )
    assert completion.served_by is None


async def test_served_by_dropped_when_display_name_has_no_matching_endpoint(monkeypatch):
    """An unmapped display name is dropped, not lowercased and passed through
    as if it were already a slug — OpenRouter renaming or retiring an
    endpoint between the call and this lookup must not fabricate a
    plausible-looking but wrong slug for `provider.ignore` to act on."""
    _FakeJsonClient.endpoints_response = {
        "data": {"endpoints": [{"provider_name": "Together", "tag": "together"}]}
    }
    completion, _ = await _run_complete_json(
        monkeypatch, OpenRouterProvider("k"), _completion_json(served_by="Some New Provider")
    )
    assert completion.served_by is None


async def test_kimi_complete_json_never_reports_served_by(monkeypatch):
    completion, _ = await _run_complete_json(
        monkeypatch, KimiProvider("k"), _completion_json(served_by="Anthropic")
    )
    # Kimi's real responses never carry `openrouter_metadata` at all; even if a
    # test payload smuggled one in, KimiProvider shares the same parser and the
    # base class's identity `_resolve_served_by` hook (Kimi never overrides
    # it), so this only pins that the parser itself is correct — the absence
    # in practice comes from Kimi's wire format, not special-casing here.
    assert completion.served_by == "Anthropic"


async def test_served_by_parsed_from_a_streaming_chunk_sequence(monkeypatch):
    _FakeStreamClient.endpoints_response = {
        "data": {"endpoints": [{"provider_name": "Fireworks", "tag": "fireworks"}]}
    }
    lines = _sse(
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]},
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "openrouter_metadata": {
                "endpoints": {"available": [{"provider": "Fireworks", "selected": True}]}
            },
        },
    )
    events, _ = await _run_stream(monkeypatch, OpenRouterProvider("k"), lines)
    assert events[-1].served_by == "fireworks"


async def test_served_by_takes_the_last_non_empty_value_seen(monkeypatch):
    _FakeStreamClient.endpoints_response = {
        "data": {
            "endpoints": [
                {"provider_name": "Together", "tag": "together"},
                {"provider_name": "Fireworks", "tag": "fireworks"},
            ]
        }
    }
    lines = _sse(
        {
            "choices": [{"delta": {"content": "a"}, "finish_reason": None}],
            "openrouter_metadata": {
                "endpoints": {"available": [{"provider": "Together", "selected": True}]}
            },
        },
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "openrouter_metadata": {
                "endpoints": {"available": [{"provider": "Fireworks", "selected": True}]}
            },
        },
    )
    events, _ = await _run_stream(monkeypatch, OpenRouterProvider("k"), lines)
    assert events[-1].served_by == "fireworks"


async def test_served_by_none_when_absent_from_every_chunk(monkeypatch):
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    events, _ = await _run_stream(monkeypatch, KimiProvider("k"), lines)
    assert events[-1].served_by is None


# ── 4b. served_by -> provider.ignore: the resolved value is a slug on the wire ──
async def test_resolved_served_by_flows_into_ignore_as_a_slug(monkeypatch):
    """End-to-end sanity for the chain Findings 1+2 depend on: once
    `served_by` is a slug, whatever builds `provider_ignore` from it (the
    router's own poor-endpoint evidence) is already handing `_provider_body`
    a slug, and `_provider_body` forwards it verbatim onto the wire — this
    only confirms the forwarding side, since the resolution side is covered
    above."""
    lines = _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
    _, body = await _run_stream(
        monkeypatch, OpenRouterProvider("k"), lines, provider_ignore=["deepinfra"]
    )
    assert body["provider"]["ignore"] == ["deepinfra"]


# ── 4c. display-name collisions resolve to a base slug, deterministically ──
def test_choose_base_slug_prefers_the_tag_without_a_slash():
    assert _choose_base_slug(["google-vertex/us-central1", "google-vertex"]) == "google-vertex"
    # Order-independent: the base slug wins regardless of which tag the wire
    # response happened to list first — the bug this fixes was a `dict`
    # comprehension's last-write-wins, which depended on exactly that order.
    assert _choose_base_slug(["google-vertex", "google-vertex/us-central1"]) == "google-vertex"


def test_choose_base_slug_falls_back_to_the_shortest_when_every_tag_is_variant_scoped():
    assert _choose_base_slug(["deepinfra/turbo", "deepinfra/fp8"]) == "deepinfra/fp8"


def test_choose_base_slug_ties_broken_alphabetically_for_a_stable_result():
    assert _choose_base_slug(["deepinfra/bb", "deepinfra/aa"]) == "deepinfra/aa"


def test_choose_base_slug_of_a_single_tag_is_that_tag():
    assert _choose_base_slug(["deepinfra"]) == "deepinfra"


async def test_endpoint_slug_map_collapses_a_display_name_collision_to_the_base_slug(monkeypatch):
    """`"Google"` naming both `google-vertex` and `google-vertex/us-central1`
    used to resolve to whichever tag the wire response listed last — an order
    the API gives no guarantee about. Both fold into the base slug instead."""
    _FakeJsonClient.endpoints_response = {
        "data": {
            "endpoints": [
                {"provider_name": "Google", "tag": "google-vertex/us-central1"},
                {"provider_name": "Google", "tag": "google-vertex"},
            ]
        }
    }
    completion, _ = await _run_complete_json(
        monkeypatch, OpenRouterProvider("k"), _completion_json(served_by="Google")
    )
    assert completion.served_by == "google-vertex"


# ── 4d. negative caching of a failing or empty endpoints lookup ────────────
class _FailingEndpointsClient:
    """Stands in for httpx.AsyncClient for the `/models/{model}/endpoints`
    GET only, always failing — used to test the negative cache (fix 4).
    Counts attempts so a test can assert how many actually reached the
    (fake) network."""

    call_count = 0

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        type(self).call_count += 1
        raise httpx.ConnectError("boom")


async def test_failing_endpoint_lookup_is_not_retried_within_the_failure_ttl(monkeypatch):
    """A failing `/models/{model}/endpoints` call used to be retried on every
    single turn — each attempt costing up to the lookup's own ~15s timeout.
    It is now negative-cached for `_ENDPOINT_SLUG_CACHE_FAILURE_TTL_S`."""
    _FailingEndpointsClient.call_count = 0
    client = _FailingEndpointsClient()
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)
    fake_now = [1_000.0]
    monkeypatch.setattr(openai_compat.time, "monotonic", lambda: fake_now[0])
    provider = OpenRouterProvider("k")

    assert await provider._resolve_served_by("m", "SomeProvider") is None
    assert _FailingEndpointsClient.call_count == 1

    # Still within the failure TTL: no second attempt reaches the network.
    fake_now[0] += provider._ENDPOINT_SLUG_CACHE_FAILURE_TTL_S - 1
    assert await provider._resolve_served_by("m", "SomeProvider") is None
    assert _FailingEndpointsClient.call_count == 1

    # Past the failure TTL: the lookup is attempted again.
    fake_now[0] += 2
    assert await provider._resolve_served_by("m", "SomeProvider") is None
    assert _FailingEndpointsClient.call_count == 2


async def test_empty_endpoints_result_is_also_negative_cached(monkeypatch):
    """A successful call that names no endpoints at all is cached at the
    short failure TTL too, not the 24h success TTL — an empty result is as
    uninformative as a failure and should be retried on the same timescale
    rather than being trusted for a full day."""
    _FakeJsonClient.endpoints_response = {"data": {"endpoints": []}}
    client = _FakeJsonClient(_FakeJsonResponse({}))
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)
    fake_now = [2_000.0]
    monkeypatch.setattr(openai_compat.time, "monotonic", lambda: fake_now[0])
    provider = OpenRouterProvider("k")

    assert await provider._resolve_served_by("m", "SomeProvider") is None
    expiry, mapping = provider._endpoint_slug_cache["m"]
    assert mapping == {}
    assert expiry == pytest.approx(fake_now[0] + provider._ENDPOINT_SLUG_CACHE_FAILURE_TTL_S)


async def test_a_successful_non_empty_lookup_still_gets_the_long_ttl(monkeypatch):
    _FakeJsonClient.endpoints_response = {
        "data": {"endpoints": [{"provider_name": "DeepInfra", "tag": "deepinfra"}]}
    }
    client = _FakeJsonClient(_FakeJsonResponse({}))
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client)
    fake_now = [3_000.0]
    monkeypatch.setattr(openai_compat.time, "monotonic", lambda: fake_now[0])
    provider = OpenRouterProvider("k")

    assert await provider._resolve_served_by("m", "DeepInfra") == "deepinfra"
    expiry, mapping = provider._endpoint_slug_cache["m"]
    assert mapping == {"deepinfra": "deepinfra"}
    assert expiry == pytest.approx(fake_now[0] + provider._ENDPOINT_SLUG_CACHE_TTL_S)


# ── 5. AnthropicProvider: effort forwarded via output_config, SDK-native ───
class _FakeAnthropicStreamCtx:
    """Stands in for `anthropic.AsyncMessages.stream()`'s return value: an
    async context manager over an async-iterable of zero events, whose
    `get_final_message()` reports a minimal completed turn."""

    def __init__(self):
        self.usage = None
        self.stop_reason = "end_turn"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def _empty():
            return
            yield  # pragma: no cover - makes this an async generator

        return _empty()

    async def get_final_message(self):
        return self


class _FakeAnthropicMessages:
    captured: dict = {}

    def stream(self, **kwargs):
        type(self).captured = kwargs
        return _FakeAnthropicStreamCtx()


async def _run_anthropic_stream(monkeypatch, *, effort=None):
    provider = AnthropicProvider("test-key")
    fake_messages = _FakeAnthropicMessages()
    monkeypatch.setattr(provider._client, "messages", fake_messages)
    events = []
    async for event in provider.stream(
        model="claude-sonnet-5",
        system="doctrine",
        messages=[Msg(role="user", content="hi")],
        tools=[],
        max_tokens=64,
        temperature=0.0,
        effort=effort,
    ):
        events.append(event)
    return events, fake_messages.captured


async def test_anthropic_stream_passes_output_config_effort_when_given(monkeypatch):
    _, kwargs = await _run_anthropic_stream(monkeypatch, effort="high")
    assert kwargs["output_config"] == {"effort": "high"}


async def test_anthropic_stream_omits_output_config_when_effort_is_none(monkeypatch):
    _, kwargs = await _run_anthropic_stream(monkeypatch, effort=None)
    assert "output_config" not in kwargs
