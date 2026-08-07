"""Unified model catalog + provider registry.

The catalog is the single source the router candidates, UI pickers, and cost
accounting all read. Static curated entries (models.yaml) always win; the
optional dynamic OpenRouter fetch adds clearly-marked "uncurated" entries.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import httpx
import yaml

from bench.config import get_settings
from bench.providers.anthropic import AnthropicProvider
from bench.providers.base import Provider
from bench.providers.local import LocalProvider
from bench.providers.openai_compat import KimiProvider, OpenRouterProvider
from bench.services.emissions import (
    DEFAULT_ENERGY_CLASS,
    ENERGY_CACHE_READ_MULTIPLIER,
    ENERGY_CACHE_WRITE_MULTIPLIER,
    ENERGY_CLASS_WH_PER_MTOK,
    ENERGY_CLASSES,
    ENERGY_TOKEN_WEIGHT_INPUT,
    ENERGY_TOKEN_WEIGHT_OUTPUT,
    REASONING_ENERGY_CLASS,
    co2e_grams,
    energy_accounting,
    energy_class_for_tier,
    energy_wh_by_bucket,
    is_reasoning_class,
    weighted_tokens,
    wh_per_mtok_for_class,
    wh_per_mtok_for_model,
)

log = logging.getLogger("bench")

_MODELS_YAML = Path(__file__).parent / "models.yaml"
_OPENROUTER_CACHE_TTL = 60 * 60 * 24  # 24h
_LOCAL_CACHE_TTL = 60 * 5  # 5min — local models/servers change far more often
_LOCAL_PROBE_TIMEOUT = 8.0

# Trivial forced-tool-call used to probe whether a local model actually honors
# tool calling (many claim OpenAI compat but ignore `tools`/`tool_choice`, or
# hallucinate arguments instead of emitting a real tool_calls entry).
_TOOL_PROBE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean", "description": "Always true."}},
    "required": ["ok"],
}

# Values a model may answer the probe's boolean `ok` with and still count as
# having filled the schema in. `True` is the only correct answer; the strings and
# 1 are here because several local runtimes stringify or integer-ify booleans on
# the way out of the tool-call serializer, and rejecting those would exclude
# genuinely tool-capable models from routing. Everything else — a missing key, an
# empty object, `false`, null — is a failed probe: an empty `arguments` payload is
# exactly what a model that ignores `tools` produces, so accepting any dict at all
# marked those models tool-capable and let them into router candidacy, which is
# the one thing the probe exists to prevent.
_PROBE_TRUTHY = (True, 1, "true", "yes", "1")


def _probe_answered(result) -> bool:
    """Did the probe come back with the required field actually filled in?"""
    if not isinstance(result, dict):
        return False
    for key in _TOOL_PROBE_SCHEMA["required"]:
        value = result.get(key)
        if isinstance(value, str):
            value = value.strip().lower()
        if value not in _PROBE_TRUTHY:
            return False
    return True


# Prompt-cache pricing, expressed as multiples of a model's input price. These are
# Anthropic's published ratios (reads 0.1x, writes 1.25x for the 5-minute TTL);
# other providers are close enough that using them as the default is honest.
CACHE_READ_MULTIPLIER = Decimal("0.1")
CACHE_WRITE_MULTIPLIER = Decimal("1.25")

# ── ecological accounting ────────────────────────────────────────────────────
# The energy/carbon model itself now lives in bench/services/emissions.py, which
# also owns PUE, the GHG Protocol scope split and the frontier-baseline
# counterfactual. The names below are re-exported here unchanged so that
# `from bench.providers.catalog import energy_accounting, ENERGY_CLASS_WH_PER_MTOK,
# ...` keeps working — the catalog is where callers have always looked for them.
# New code should import from bench.services.emissions directly.
__all__ = [
    "CACHE_READ_MULTIPLIER",
    "CACHE_WRITE_MULTIPLIER",
    "DEFAULT_ENERGY_CLASS",
    "ENERGY_CACHE_READ_MULTIPLIER",
    "ENERGY_CACHE_WRITE_MULTIPLIER",
    "ENERGY_CLASSES",
    "ENERGY_CLASS_WH_PER_MTOK",
    "ENERGY_TOKEN_WEIGHT_INPUT",
    "ENERGY_TOKEN_WEIGHT_OUTPUT",
    "KEY_PROVIDERS",
    "PROVIDER_NAMES",
    "PROVIDER_SPECS",
    "REASONING_ENERGY_CLASS",
    "LocalDiscovery",
    "ModelCatalog",
    "ModelInfo",
    "ProviderRegistry",
    "ProviderSpec",
    "co2e_grams",
    "energy_accounting",
    "energy_class_for_tier",
    "energy_wh_by_bucket",
    "get_catalog",
    "is_reasoning_class",
    "weighted_tokens",
    "wh_per_mtok_for_class",
    "wh_per_mtok_for_model",
]


@dataclass
class ModelInfo:
    id: str  # "anthropic/claude-sonnet-5" — bench-wide id, provider-prefixed
    provider: str  # anthropic | kimi | openrouter
    wire_id: str  # what the provider API receives
    display_name: str
    context_window: int
    input_price_per_mtok: Decimal
    output_price_per_mtok: Decimal
    cost_tier: str  # economy | standard | premium | local (always allowed, see TIER_ORDER)
    strengths: list[str] = field(default_factory=list)
    supports_tools: bool = True
    curated: bool = True
    released: str | None = None  # YYYY-MM; feeds the router's prefer-newer rule
    # S | M | L | XL | R — calibrated energy bucket (see ENERGY_CLASS_WH_PER_MTOK).
    # R is the reasoning tier and must be assigned deliberately: price does not
    # predict it in either direction.
    energy_class: str = DEFAULT_ENERGY_CLASS
    # Wh per million *output-equivalent* tokens (an output token is 1.0, an input
    # token 0.05 — see emissions.weighted_tokens). Passing None means "derive
    # from energy_class"; after construction this is always a Decimal.
    energy_wh_per_mtok: Decimal | None = None

    def __post_init__(self) -> None:
        if self.energy_class not in ENERGY_CLASS_WH_PER_MTOK:
            self.energy_class = DEFAULT_ENERGY_CLASS
        if self.energy_wh_per_mtok is None:
            # Via the emissions seam rather than the class table directly, so a
            # future size-based estimator (EcoLogits' active-parameter formula,
            # say) reaches every catalog entry by changing one function.
            self.energy_wh_per_mtok = wh_per_mtok_for_model(self)

    @property
    def is_reasoning_tier(self) -> bool:
        """Does this model spend thinking tokens before answering?

        Matters beyond the energy figure: for several providers hidden reasoning
        tokens are absent from the billed output count bench reads, so a
        reasoning model's real generation work is undercounted.
        """
        return is_reasoning_class(self.energy_class)

    def energy_wh(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> Decimal:
        """Estimated *compute* (IT-load) energy for one turn, in watt-hours.

        Excludes data-centre overhead: multiply by the deployment's PUE for the
        total (bench.services.emissions.pue_for). Every token bucket draws power,
        but not equally: generation costs roughly 20x reading per token, so
        `weighted_tokens` converts each bucket to output-equivalents first.
        Estimated, never measured: docs/emissions-methodology.md.
        """
        weighted = weighted_tokens(
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
        )
        return self.energy_wh_per_mtok * weighted / Decimal(1_000_000)

    def energy_wh_by_bucket(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> dict[str, Decimal]:
        """Compute Wh split per token bucket; sums to `energy_wh`."""
        return energy_wh_by_bucket(
            self.energy_wh_per_mtok,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
        )

    def cost_usd(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        *,
        cache_read_multiplier: Decimal = CACHE_READ_MULTIPLIER,
        cache_write_multiplier: Decimal = CACHE_WRITE_MULTIPLIER,
    ) -> Decimal:
        """Cost of one turn. `input_tokens` must exclude the cache buckets."""
        return (
            self.input_price_per_mtok * input_tokens
            + self.output_price_per_mtok * output_tokens
            + self.input_price_per_mtok * cache_read_multiplier * cache_read_tokens
            + self.input_price_per_mtok * cache_write_multiplier * cache_write_tokens
        ) / Decimal(1_000_000)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "provider": self.provider,
            "display_name": self.display_name,
            "context_window": self.context_window,
            "input_price_per_mtok": float(self.input_price_per_mtok),
            "output_price_per_mtok": float(self.output_price_per_mtok),
            "cost_tier": self.cost_tier,
            "strengths": self.strengths,
            "supports_tools": self.supports_tools,
            "curated": self.curated,
            "released": self.released,
            "energy_class": self.energy_class,
            "energy_wh_per_mtok": float(self.energy_wh_per_mtok),
            # Added: the per-bucket figures, so a picker can show that a
            # long-prompt task costs far less than a long-answer one.
            "energy_wh_per_mtok_input": float(
                self.energy_wh_per_mtok * ENERGY_TOKEN_WEIGHT_INPUT
            ),
            "energy_wh_per_mtok_output": float(
                self.energy_wh_per_mtok * ENERGY_TOKEN_WEIGHT_OUTPUT
            ),
            "reasoning_tier": self.is_reasoning_tier,
        }


@dataclass
class LocalDiscovery:
    """Outcome of one local-server discovery pass — the diagnostic view of it.

    `refresh_local` has always been best-effort and silent (an unreachable
    server just yields an empty local catalog). The settings "test connection"
    endpoint needs to tell *why* nothing showed up, so discovery reports its
    outcome here instead of only mutating catalog state. Existing callers ignore
    the return value and behave exactly as before.
    """

    configured: bool
    base_url: str
    reachable: bool
    error: str | None = None
    models: list[ModelInfo] = field(default_factory=list)
    cached: bool = False


def _describe_error(exc: Exception, limit: int = 300) -> str:
    """`ConnectError: [Errno 61] Connection refused` — class plus message, truncated."""
    message = f"{type(exc).__name__}: {exc}".strip()
    return message[:limit] if len(message) > limit else message


def _usable_price(price: Decimal) -> bool:
    """Is this a price bench can tier and bill against? Finite and >= 0."""
    return price.is_finite() and price >= 0


def _tier_from_price(output_price: Decimal) -> str:
    if output_price >= Decimal("30"):
        return "premium"
    if output_price >= Decimal("4"):
        return "standard"
    return "economy"


class ModelCatalog:
    def __init__(self) -> None:
        self._static = self._load_static()
        self._dynamic: dict[str, ModelInfo] = {}
        self._dynamic_fetched_at: float = 0.0
        self._local: dict[str, ModelInfo] = {}
        self._local_fetched_at: float = 0.0
        # Tool-support probe results, keyed by bench model id. Populated lazily
        # and kept for the process lifetime — a local model's tool support is a
        # property of the running server/weights, not something that flips
        # minute to minute, so there is no need to re-probe on every refresh.
        self._tool_probe_cache: dict[str, bool] = {}
        # Has a discovery pass (local + dynamic) been attempted in this process?
        # See `warm_once`.
        self._warmed = False

    async def warm(self) -> None:
        """Run the discovery passes the catalog needs to be complete.

        The curated static entries exist from import; local and dynamic
        OpenRouter entries exist only after a discovery pass, which used to
        happen *only* inside GET /api/models. On a freshly booted process a
        harness run could therefore not route to a local model — or to any
        dynamic one — until somebody opened the UI, which is not a dependency a
        headless run should have. bench/main.py schedules this at startup as a
        background task (never awaited, so it adds nothing to boot time) and
        `warm_once` is the backstop for a run that beats it.

        Best-effort throughout: both passes already treat an unreachable server
        as "no models", and anything they do raise is logged, never propagated —
        a failed refresh must not fail the caller that triggered it.
        """
        self._warmed = True
        try:
            await self.refresh_dynamic()
        except Exception:  # noqa: BLE001 - warming is never fatal
            log.warning("Dynamic model catalog refresh failed; continuing", exc_info=True)
        # Only when configured: refresh_local() with no base URL *clears* the
        # local catalog, which is the right behaviour for the settings path and
        # the wrong one for a warm-up.
        if get_settings().local_base_url:
            try:
                await self.refresh_local()
            except Exception:  # noqa: BLE001 - warming is never fatal
                log.warning("Local model discovery failed; continuing", exc_info=True)

    async def warm_once(self) -> None:
        """`warm()` unless some pass already ran in this process."""
        if self._warmed:
            return
        await self.warm()

    @staticmethod
    def _load_static() -> dict[str, ModelInfo]:
        raw = yaml.safe_load(_MODELS_YAML.read_text())
        out: dict[str, ModelInfo] = {}
        for m in raw["models"]:
            wh_override = m.get("energy_wh_per_mtok")
            info = ModelInfo(
                id=m["id"],
                provider=m["provider"],
                wire_id=m["wire_id"],
                display_name=m["display_name"],
                context_window=m["context_window"],
                input_price_per_mtok=Decimal(str(m["input_price_per_mtok"])),
                output_price_per_mtok=Decimal(str(m["output_price_per_mtok"])),
                cost_tier=m["cost_tier"],
                strengths=m.get("strengths", []),
                supports_tools=m.get("supports_tools", True),
                curated=True,
                released=str(m["released"]) if m.get("released") else None,
                # Unclassified curated entries fall back to their cost tier's
                # estimated class rather than silently reading as low-energy.
                energy_class=m.get("energy_class") or energy_class_for_tier(m["cost_tier"]),
                energy_wh_per_mtok=Decimal(str(wh_override)) if wh_override else None,
            )
            out[info.id] = info
        return out

    async def refresh_dynamic(self) -> None:
        """Fetch the OpenRouter catalog (optional, cached 24h). Static wins."""
        settings = get_settings()
        if not settings.openrouter_catalog or not settings.openrouter_api_key:
            return
        if time.monotonic() - self._dynamic_fetched_at < _OPENROUTER_CACHE_TTL and self._dynamic:
            return
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get("https://openrouter.ai/api/v1/models")
            resp.raise_for_status()
            # Parsed inside the try: a 200 that is not JSON (a captive portal or
            # proxy error page, an HTML maintenance notice) raises ValueError, and
            # outside the try that made the whole of GET /api/models fail — the
            # curated catalog with it — instead of degrading to "no dynamic
            # models", which is what "best-effort" has to mean.
            payload = resp.json()
        except (httpx.HTTPError, ValueError):
            return  # dynamic catalog is best-effort
        if not isinstance(payload, dict):
            return
        entries = payload.get("data")
        if not isinstance(entries, list):
            return
        dynamic: dict[str, ModelInfo] = {}
        for m in entries:
            if not isinstance(m, dict):
                continue
            wire_id = m.get("id", "")
            bench_id = f"openrouter/{wire_id}"
            if not wire_id or bench_id in self._static:
                continue
            supported = m.get("supported_parameters") or []
            if "tools" not in supported:
                continue
            pricing = m.get("pricing", {})
            if not isinstance(pricing, dict):
                continue
            try:
                in_price = Decimal(str(pricing.get("prompt", "0"))) * Decimal(1_000_000)
                out_price = Decimal(str(pricing.get("completion", "0"))) * Decimal(1_000_000)
            except Exception:
                continue
            if not _usable_price(in_price) or not _usable_price(out_price):
                # OpenRouter uses "-1" as a sentinel for variable/unknown pricing
                # (auto-routers, some BYOK entries), and Decimal happily accepts
                # "NaN"/"Infinity" as well. Any of those would put the entry in a
                # cost tier by accident and then feed a nonsense number into cost
                # accounting, so the entry is skipped: an absent model is honest,
                # a negatively-priced one is not. Zero is kept — free models are
                # real, and their energy figure is still positive.
                continue
            created = m.get("created")
            released = None
            if created:
                try:
                    from datetime import datetime, timezone

                    released = datetime.fromtimestamp(int(created), tz=timezone.utc).strftime("%Y-%m")
                except (ValueError, OSError):
                    pass
            dynamic[bench_id] = ModelInfo(
                id=bench_id,
                provider="openrouter",
                wire_id=wire_id,
                display_name=m.get("name", wire_id),
                context_window=int(m.get("context_length") or 0),
                input_price_per_mtok=in_price,
                output_price_per_mtok=out_price,
                cost_tier=_tier_from_price(out_price),
                strengths=[],
                supports_tools=True,
                curated=False,
                released=released,
                # Nobody has classified these by hand: estimate from the price
                # tier (economy→M, standard→L, premium→XL).
                energy_class=energy_class_for_tier(_tier_from_price(out_price)),
            )
        self._dynamic = dynamic
        self._dynamic_fetched_at = time.monotonic()

    async def refresh_local(self, *, force: bool = False) -> LocalDiscovery:
        """Discover models from a configured local OpenAI-compat server.

        Best-effort and short-cached (5 min, vs. 24h for OpenRouter): local
        servers get models swapped in and out far more often than a cloud
        catalog, but polling them is also free and local, so a short TTL is
        cheap. An unreachable/misconfigured server yields an empty local
        catalog rather than raising — bench must keep working with zero local
        models the same way it works with zero OpenRouter models.

        `force=True` is the diagnostic path used by the settings "test
        connection" button: it bypasses both the 5-minute TTL and the
        per-process tool-probe cache, so the operator sees the server as it is
        right now rather than as it was when this process first looked. Normal
        callers pass nothing and get the cached, probe-once behaviour unchanged.
        """
        settings = get_settings()
        if not settings.local_base_url:
            self._local = {}
            return LocalDiscovery(configured=False, base_url="", reachable=False)
        configured_url = settings.local_base_url
        if (
            not force
            and time.monotonic() - self._local_fetched_at < _LOCAL_CACHE_TTL
            and self._local
        ):
            return LocalDiscovery(
                configured=True,
                base_url=configured_url,
                reachable=True,
                models=list(self._local.values()),
                cached=True,
            )
        base_url = configured_url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{base_url}/models",
                    headers={"Authorization": f"Bearer {settings.local_api_key or 'local'}"},
                )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            self._local = {}
            self._local_fetched_at = time.monotonic()
            return LocalDiscovery(
                configured=True,
                base_url=configured_url,
                reachable=False,
                error=_describe_error(exc),
            )

        provider = LocalProvider(
            base_url=settings.local_base_url,
            api_key=settings.local_api_key,
            display_name=settings.local_display_name,
        )
        discovered: dict[str, ModelInfo] = {}
        for m in payload.get("data") or []:
            wire_id = m.get("id", "")
            if not wire_id:
                continue
            bench_id = f"local/{wire_id}"
            context_window = 0
            for key in ("context_length", "max_model_len", "context_window"):
                value = m.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                    context_window = int(value)
                    break
            supports_tools = await self._probe_supports_tools(
                provider, bench_id, wire_id, settings, force=force
            )
            discovered[bench_id] = ModelInfo(
                id=bench_id,
                provider="local",
                wire_id=wire_id,
                display_name=f"{settings.local_display_name}: {wire_id}",
                context_window=context_window,
                input_price_per_mtok=Decimal("0"),
                output_price_per_mtok=Decimal("0"),
                cost_tier="local",
                strengths=[],
                supports_tools=supports_tools,
                curated=False,
                released=None,
                # Local inference is free in dollars but never free in watts:
                # S is the estimate for small quantized weights on end-user
                # hardware, and it is still a positive number.
                energy_class=energy_class_for_tier("local"),
            )
        self._local = discovered
        self._local_fetched_at = time.monotonic()
        return LocalDiscovery(
            configured=True,
            base_url=configured_url,
            reachable=True,
            models=list(discovered.values()),
        )

    async def _probe_supports_tools(
        self,
        provider: LocalProvider,
        bench_id: str,
        wire_id: str,
        settings,
        *,
        force: bool = False,
    ) -> bool:
        """Cheap forced-tool-call probe, cached per model id.

        Local models routinely advertise an OpenAI-compat surface without
        actually honoring `tools`/`tool_choice`. Since bench's trust model
        (grading, structured extraction, terminal actions) runs entirely
        through tool calls, a model that fails this probe must never reach
        router candidacy — see router_llm/router.py's `supports_tools` filter.

        `force=True` skips the cache *read* (the result is still cached), so a
        model that has since been re-pulled or re-quantized gets a fresh verdict.
        """
        if not settings.local_probe_tools:
            return True
        if not force and bench_id in self._tool_probe_cache:
            return self._tool_probe_cache[bench_id]
        try:
            result = await provider.complete_json(
                model=wire_id,
                system="You must respond only by calling the provided tool.",
                prompt="Call the tool now with ok set to true.",
                schema=_TOOL_PROBE_SCHEMA,
                tool_name="probe_tool_support",
                max_tokens=64,
                timeout=_LOCAL_PROBE_TIMEOUT,
            )
            ok = _probe_answered(result)
        except Exception:
            ok = False
        self._tool_probe_cache[bench_id] = ok
        return ok

    def get(self, model_id: str) -> ModelInfo | None:
        return self._static.get(model_id) or self._dynamic.get(model_id) or self._local.get(
            model_id
        )

    def all(self, curated_only: bool = False) -> list[ModelInfo]:
        items = list(self._static.values())
        if not curated_only:
            items += list(self._dynamic.values())
            items += list(self._local.values())
        return items


@dataclass(frozen=True)
class ProviderSpec:
    """Everything bench needs to know about one provider, in one place.

    Adding a provider used to mean editing three parallel provider→something
    maps (the env-key map, the `has_key` special cases, and the if/elif
    construction chain), where forgetting one produced a provider that was
    "configured" but unbuildable, or buildable but never offered. One row here
    now drives all three.

    It also drives the settings endpoint: `bench/api/settings.py` derives its
    `PROVIDERS` tuple from `KEY_PROVIDERS` and its env-key map from these rows'
    `env_key_attr`, and the frontend's key picker is rendered from that
    endpoint's response rather than from a list of its own. So a new provider
    needs a row here and its models.yaml entries, and nothing else.
    """

    name: str
    # Settings attribute holding the env API key. Empty for a key-optional
    # provider, whose credential is not an API key at all.
    env_key_attr: str = ""
    # Settings attribute that enables a key-optional provider instead of a key.
    enabled_attr: str = ""
    # (api_key, settings) -> Provider. Constructed lazily, once per registry.
    factory: Callable[[str, object], Provider] = field(
        default=lambda key, settings: None, repr=False
    )

    @property
    def key_optional(self) -> bool:
        return bool(self.enabled_attr)


PROVIDER_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        name="anthropic",
        env_key_attr="anthropic_api_key",
        factory=lambda key, settings: AnthropicProvider(key),
    ),
    ProviderSpec(
        name="kimi",
        env_key_attr="moonshot_api_key",
        factory=lambda key, settings: KimiProvider(key),
    ),
    ProviderSpec(
        name="openrouter",
        env_key_attr="openrouter_api_key",
        factory=lambda key, settings: OpenRouterProvider(
            key, referer=settings.openrouter_referer, title=settings.openrouter_title
        ),
    ),
    # "local" is deliberately key-optional: a configured base URL *is* the
    # credential (most local servers ignore Authorization entirely), so a
    # local-only installation with no cloud keys anywhere still routes.
    ProviderSpec(
        name="local",
        enabled_attr="local_base_url",
        factory=lambda key, settings: LocalProvider(
            base_url=settings.local_base_url,
            api_key=settings.local_api_key,
            display_name=settings.local_display_name,
        ),
    ),
)

_SPECS_BY_NAME: dict[str, ProviderSpec] = {spec.name: spec for spec in PROVIDER_SPECS}
# Provider names in catalog order. KEY_PROVIDERS are the ones a workspace can
# store an API key for; "local" is not one of them.
PROVIDER_NAMES: tuple[str, ...] = tuple(_SPECS_BY_NAME)
KEY_PROVIDERS: tuple[str, ...] = tuple(
    spec.name for spec in PROVIDER_SPECS if not spec.key_optional
)


class ProviderRegistry:
    """Constructs providers from configured keys. Env keys win over DB keys.

    Every provider-specific fact lives in PROVIDER_SPECS above; this class is the
    generic machinery over it. "local" is the key-optional case: `has_key("local")`
    is true whenever a base URL is set, key or no key.
    """

    def __init__(self, db_keys: dict[str, str] | None = None):
        settings = get_settings()
        db_keys = db_keys or {}
        self._settings = settings
        self._keys = {
            spec.name: getattr(settings, spec.env_key_attr) or db_keys.get(spec.name, "")
            for spec in PROVIDER_SPECS
            if spec.env_key_attr
        }
        self._enabled = {
            spec.name: bool(getattr(settings, spec.enabled_attr))
            for spec in PROVIDER_SPECS
            if spec.key_optional
        }
        self._instances: dict[str, Provider] = {}

    def has_key(self, provider: str) -> bool:
        if provider in self._enabled:
            return self._enabled[provider]
        return bool(self._keys.get(provider))

    def available_providers(self) -> list[str]:
        return [spec.name for spec in PROVIDER_SPECS if self.has_key(spec.name)]

    def get(self, provider: str) -> Provider:
        if provider not in self._instances:
            spec = _SPECS_BY_NAME.get(provider)
            if spec is None:
                raise KeyError(f"Unknown provider '{provider}'")
            if not self.has_key(provider):
                raise KeyError(
                    f"No base URL configured for provider '{provider}'"
                    if spec.key_optional
                    else f"No API key configured for provider '{provider}'"
                )
            self._instances[provider] = spec.factory(self._keys.get(provider, ""), self._settings)
        return self._instances[provider]


_catalog: ModelCatalog | None = None


def get_catalog() -> ModelCatalog:
    global _catalog
    if _catalog is None:
        _catalog = ModelCatalog()
    return _catalog
