"""Unified model catalog + provider registry.

The catalog is the single source the router candidates, UI pickers, and cost
accounting all read. Static curated entries (models.yaml) always win; the
optional dynamic OpenRouter fetch adds clearly-marked "uncurated" entries.
"""
from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from tret.net import CLASS_CATALOG, CLASS_LOCAL, CLASS_PROVIDER, EgressDenied, effective_mode, open_client
from tret.net.policy import MODE_OFF
import yaml

from tret.config import get_settings
from tret.providers.anthropic import AnthropicProvider
from tret.providers.base import Provider
from tret.providers.local import LocalProvider
from tret.providers.openai_compat import KimiProvider, OpenRouterProvider
from tret.services.emissions import (
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

log = logging.getLogger("tret")

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


def _today() -> date:
    """Today's date (UTC) — the one place dated pricing reads the clock.

    `ModelInfo.prices_at()` reads this itself (via its `at=None` default) on
    every call, and `cost_usd` calls `prices_at` on every invocation — so
    freezing "today" for scheduled pricing is one `monkeypatch.setattr(this
    module, "_today", ...)` away, and it takes effect immediately against a
    `ModelCatalog`/`ModelInfo` built long before the freeze, no reload
    needed. Production code should never need to call this directly.
    """
    return datetime.now(timezone.utc).date()


@dataclass(frozen=True)
class PricingTier:
    """A whole-request price step once the prompt crosses a size threshold.

    Some 2026-era frontier models (GPT-6 Astra is the first in this catalog)
    bill the *entire* request at a different rate above a prompt-size cutoff,
    rather than only the tokens past it — closer to a tax bracket that taxes
    the whole amount at the top rate than one that only taxes the marginal
    slice. `ModelInfo.cost_usd` picks the tier whose `above_prompt_tokens` is
    the largest one still strictly below the request's prompt size.
    """

    above_prompt_tokens: int
    input_multiplier: Decimal
    output_multiplier: Decimal


@dataclass(frozen=True)
class PriceChange:
    """A scheduled price change, kept for both billing and display.

    `ModelInfo.input_price_per_mtok`/`output_price_per_mtok` are always the
    model's *base* (models.yaml) price — this schedule never overwrites them.
    `ModelInfo.prices_at()` resolves whichever entry here is due as of a given
    date (default: `_today()`), and `cost_usd` calls it fresh on every
    invocation, so a long-running process bills a scheduled change correctly
    the day it takes effect, with no reload. The raw list still lives on
    `ModelInfo.price_changes` so a picker can show "$0.75 now, $1.50 from
    2027-01-01" rather than just one number.
    """

    effective: date
    input_price_per_mtok: Decimal
    output_price_per_mtok: Decimal

# ── ecological accounting ────────────────────────────────────────────────────
# The energy/carbon model itself now lives in tret/services/emissions.py, which
# also owns PUE, the GHG Protocol scope split and the frontier-baseline
# counterfactual. The names below are re-exported here unchanged so that
# `from tret.providers.catalog import energy_accounting, ENERGY_CLASS_WH_PER_MTOK,
# ...` keeps working — the catalog is where callers have always looked for them.
# New code should import from tret.services.emissions directly.
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
    "PriceChange",
    "PricingTier",
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
    id: str  # "anthropic/claude-sonnet-5" — tret-wide id, provider-prefixed
    provider: str  # anthropic | kimi | openrouter
    wire_id: str  # what the provider API receives
    display_name: str
    context_window: int
    input_price_per_mtok: Decimal
    output_price_per_mtok: Decimal
    cost_tier: str  # economy | standard | premium | local (always allowed, see TIER_ORDER)
    strengths: list[str] = field(default_factory=list)
    supports_tools: bool = True
    # Does this model's provider API accept a reasoning-effort control at all?
    # Anthropic's `output_config.effort` and OpenRouter's unified `reasoning.
    # effort` are two different wire shapes for the same idea, and neither is
    # universal even within a provider — see models.yaml for the per-model
    # calls. The router (router_llm/router.py) records an effort level on
    # every `RoutingDecision` regardless of this flag (it documents intent);
    # this flag is read only by the harness/provider layer, which decides
    # whether to actually send it (a model with supports_effort=False gets
    # `effort=None` in the stream() call, so the provider never sends the
    # field at all rather than sending one the model would reject or ignore).
    supports_effort: bool = False
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
    # Billions of active parameters per forward pass — for a mixture-of-experts
    # model, the active subset, not the total. Unpublished for every closed
    # model here, so this is opt-in and unset by default. Read by
    # `emissions._energy_constant_and_flags` when a run's `energy_strategy` is
    # `"active_params"` and this model has no explicit `energy_wh_per_mtok`.
    active_params_b: float | None = None
    # Was `energy_wh_per_mtok` set explicitly (a real `models.yaml` constant,
    # an operator's own metered figure) rather than derived at construction by
    # the class ladder? Set in `__post_init__` *before* the bake below, so it
    # is the only reliable way to tell the two apart once construction has
    # finished — `energy_wh_per_mtok is not None` is true either way, which is
    # exactly the bug this field exists to fix (see
    # `emissions._energy_constant_and_flags`'s "explicit constant wins" rung:
    # checking `is not None` there made it fire for every model, explicit or
    # not, and the active-parameter branch below it unreachable).
    energy_wh_per_mtok_explicit: bool = False
    # Per-model overrides of the module-default cache ratios above. Anthropic
    # cut Claude Fable 5.1's cache reads to 0.025x input price while every
    # other model (including Fable 5 a point release back) stays at 0.1x, so a
    # single module constant can no longer speak for the whole catalog.
    # Defaulting to the module constants keeps every existing entry's billing
    # unchanged.
    cache_read_multiplier: Decimal = CACHE_READ_MULTIPLIER
    cache_write_multiplier: Decimal = CACHE_WRITE_MULTIPLIER
    # Whole-request price steps above a prompt-size threshold. Empty for every
    # model that bills a flat rate (i.e. almost all of them) — see PricingTier.
    pricing_tiers: list[PricingTier] = field(default_factory=list)
    # Scheduled price changes, raw — see PriceChange. input_price_per_mtok/
    # output_price_per_mtok above are always the base (models.yaml) price;
    # call `prices_at()` (or `cost_usd`, which does this internally on every
    # call) for whichever price is actually in effect on a given date.
    price_changes: list[PriceChange] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.energy_class not in ENERGY_CLASS_WH_PER_MTOK:
            self.energy_class = DEFAULT_ENERGY_CLASS
        if self.active_params_b is not None and (
            not math.isfinite(self.active_params_b) or self.active_params_b <= 0
        ):
            raise ValueError(
                f"active_params_b must be positive and finite, got {self.active_params_b!r}"
            )
        prev_threshold = -1
        for tier in self.pricing_tiers:
            if tier.above_prompt_tokens <= prev_threshold:
                raise ValueError(
                    f"{self.id}: pricing_tiers thresholds must be strictly ascending, "
                    f"got {[t.above_prompt_tokens for t in self.pricing_tiers]!r}"
                )
            if tier.input_multiplier <= 0 or tier.output_multiplier <= 0:
                raise ValueError(
                    f"{self.id}: pricing_tiers multipliers must be positive, got {tier!r}"
                )
            prev_threshold = tier.above_prompt_tokens
        prev_effective: date | None = None
        for change in self.price_changes:
            if change.input_price_per_mtok <= 0 or change.output_price_per_mtok <= 0:
                raise ValueError(
                    f"{self.id}: price_changes prices must be positive, got {change!r}"
                )
            if prev_effective is not None and change.effective <= prev_effective:
                raise ValueError(
                    f"{self.id}: price_changes must be sorted strictly ascending by "
                    f"effective date, got "
                    f"{[c.effective.isoformat() for c in self.price_changes]!r}"
                )
            prev_effective = change.effective
            # A scheduled price is a *replacement* rate, not a tweak — if it
            # would silently move the model into a different cost_tier band
            # than the one curators actually assigned it, that is a data bug,
            # not a scheduling decision, and must fail loudly at load rather
            # than mis-tier the model the day the change takes effect. Checked
            # against the same `_tier_from_price` a live OpenRouter fetch uses
            # to classify an uncurated entry.
            new_tier = _tier_from_price(change.output_price_per_mtok)
            if new_tier != self.cost_tier:
                raise ValueError(
                    f"{self.id}: price_changes entry effective "
                    f"{change.effective.isoformat()} prices output at "
                    f"${change.output_price_per_mtok}/Mtok, which is '{new_tier}' tier, "
                    f"not this model's declared cost_tier {self.cost_tier!r}"
                )
        self.energy_wh_per_mtok_explicit = self.energy_wh_per_mtok is not None
        if self.energy_wh_per_mtok is None:
            # Via the emissions seam rather than the class table directly, so a
            # future size-based estimator (EcoLogits' active-parameter formula,
            # say) reaches every catalog entry by changing one function.
            self.energy_wh_per_mtok = wh_per_mtok_for_model(self)

    @property
    def is_reasoning_tier(self) -> bool:
        """Does this model spend thinking tokens before answering?

        Matters beyond the energy figure: for several providers hidden reasoning
        tokens are absent from the billed output count tret reads, so a
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
        total (tret.services.emissions.pue_for). Every token bucket draws power,
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

    def _pricing_tier_for(self, prompt_tokens: int) -> PricingTier | None:
        """The tier whose threshold is the largest one still below `prompt_tokens`.

        "Strictly below", i.e. `above_prompt_tokens < prompt_tokens`: a prompt
        sitting exactly on the threshold has not yet crossed it. This matches
        GPT-6 Astra both ways — OpenAI's own pricing bills "requests over 272K
        input tokens" (exclusive, not "at or over"), and the live OpenRouter
        catalog entry (https://openrouter.ai/api/v1/models, id
        "openai/gpt-6-astra") applies its `pricing.overrides[0]`
        (`min_prompt_tokens: 272000`) the same way: a 272,000-token prompt
        still bills at the base rate, and 272,001 is the first count that gets
        the override. `above_prompt_tokens: 272000` in models.yaml is
        therefore correct as written — see the comment on that entry.
        """
        applicable = [t for t in self.pricing_tiers if t.above_prompt_tokens < prompt_tokens]
        return max(applicable, key=lambda t: t.above_prompt_tokens) if applicable else None

    def prices_at(self, at: date | None = None) -> tuple[Decimal, Decimal]:
        """The (input, output) price per Mtok actually in effect on `at`.

        `input_price_per_mtok`/`output_price_per_mtok` are always this
        model's *base* (models.yaml) price; `price_changes` is a schedule of
        future adjustments. This is the one place that reconciles them, and
        it does so at *call* time rather than once at catalog-load time —
        `cost_usd` calls it fresh on every invocation with `at=None` (today),
        so a long-running process bills a scheduled change correctly the day
        it takes effect, with no reload. `at=None` resolves to `_today()`,
        the same injectable seam every other dated-pricing read in this
        module uses.
        """
        if at is None:
            at = _today()
        due = [pc for pc in self.price_changes if pc.effective <= at]
        if not due:
            return self.input_price_per_mtok, self.output_price_per_mtok
        current = max(due, key=lambda pc: pc.effective)
        return current.input_price_per_mtok, current.output_price_per_mtok

    def cost_usd(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        *,
        cache_read_multiplier: Decimal | None = None,
        cache_write_multiplier: Decimal | None = None,
    ) -> Decimal:
        """Cost of one turn. `input_tokens` must exclude the cache buckets.

        `cache_read_multiplier`/`cache_write_multiplier` default to this
        model's own fields — CACHE_READ_MULTIPLIER/CACHE_WRITE_MULTIPLIER
        unless the catalog entry overrides them (Claude Fable 5.1's 0.025x
        cache reads) — so per-model billing works without every caller having
        to know the override exists. An explicit value here still wins, for a
        caller deliberately pricing a hypothetical rate.

        Some models bill the *whole* request at a different rate once the
        prompt crosses a size threshold (see `pricing_tiers`): the tier's
        `input_multiplier` scales the input, cache-read and cache-write terms,
        and `output_multiplier` scales the output term. `prompt_tokens` for
        that decision is `input + cache_read + cache_write` — the provider's
        own definition of "how big is this request", not input alone.

        Reads `prices_at(_today())` rather than `input_price_per_mtok`/
        `output_price_per_mtok` directly, so a scheduled `price_changes`
        entry that has come due since this `ModelInfo` was constructed still
        bills correctly — no catalog reload required.
        """
        if cache_read_multiplier is None:
            cache_read_multiplier = self.cache_read_multiplier
        if cache_write_multiplier is None:
            cache_write_multiplier = self.cache_write_multiplier

        input_price, output_price = self.prices_at(_today())
        prompt_tokens = input_tokens + cache_read_tokens + cache_write_tokens
        tier = self._pricing_tier_for(prompt_tokens)
        input_mult = tier.input_multiplier if tier else Decimal(1)
        output_mult = tier.output_multiplier if tier else Decimal(1)

        return (
            input_price * input_mult * input_tokens
            + output_price * output_mult * output_tokens
            + input_price * input_mult * cache_read_multiplier * cache_read_tokens
            + input_price * input_mult * cache_write_multiplier * cache_write_tokens
        ) / Decimal(1_000_000)

    def to_json(self) -> dict:
        # The price a picker should show as "now" is the *effective* one, not
        # the base models.yaml figure — same resolution cost_usd uses, so the
        # UI and the bill it is about to run up always agree.
        input_price, output_price = self.prices_at()
        return {
            "id": self.id,
            "provider": self.provider,
            "display_name": self.display_name,
            "context_window": self.context_window,
            "input_price_per_mtok": float(input_price),
            "output_price_per_mtok": float(output_price),
            "cost_tier": self.cost_tier,
            "strengths": self.strengths,
            "supports_tools": self.supports_tools,
            "supports_effort": self.supports_effort,
            "curated": self.curated,
            "released": self.released,
            "energy_class": self.energy_class,
            "energy_wh_per_mtok": float(self.energy_wh_per_mtok),
            "energy_wh_per_mtok_explicit": self.energy_wh_per_mtok_explicit,
            # Added: the per-bucket figures, so a picker can show that a
            # long-prompt task costs far less than a long-answer one.
            "energy_wh_per_mtok_input": float(
                self.energy_wh_per_mtok * ENERGY_TOKEN_WEIGHT_INPUT
            ),
            "energy_wh_per_mtok_output": float(
                self.energy_wh_per_mtok * ENERGY_TOKEN_WEIGHT_OUTPUT
            ),
            "reasoning_tier": self.is_reasoning_tier,
            "cache_read_multiplier": float(self.cache_read_multiplier),
            "cache_write_multiplier": float(self.cache_write_multiplier),
            "pricing_tiers": [
                {
                    "above_prompt_tokens": t.above_prompt_tokens,
                    "input_multiplier": float(t.input_multiplier),
                    "output_multiplier": float(t.output_multiplier),
                }
                for t in self.pricing_tiers
            ],
            "price_changes": [
                {
                    "effective": pc.effective.isoformat(),
                    "input_price_per_mtok": float(pc.input_price_per_mtok),
                    "output_price_per_mtok": float(pc.output_price_per_mtok),
                }
                for pc in self.price_changes
            ],
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
    """Is this a price tret can tier and bill against? Finite and >= 0."""
    return price.is_finite() and price >= 0


def _tier_from_price(output_price: Decimal) -> str:
    if output_price >= Decimal("30"):
        return "premium"
    if output_price >= Decimal("4"):
        return "standard"
    return "economy"


def _parse_price_change_date(model_id: str, pc: dict) -> date:
    """A `price_changes` entry's `effective` value, as a clear ValueError on
    anything that is not an ISO `YYYY-MM-DD` date — `date.fromisoformat` on a
    malformed yaml value raises one already; this just names the model."""
    raw = pc.get("effective")
    try:
        return date.fromisoformat(str(raw))
    except ValueError as exc:
        raise ValueError(
            f"{model_id}: price_changes effective date {raw!r} is not a valid "
            "ISO date (YYYY-MM-DD)"
        ) from exc


def _parse_price_change_price(model_id: str, pc: dict, field: str) -> Decimal:
    """A `price_changes` entry's price field, as a clear ValueError on
    anything `Decimal` cannot parse — a bad numeric yaml value otherwise
    raises `decimal.InvalidOperation`, which is not a `ValueError`."""
    raw = pc.get(field)
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(
            f"{model_id}: price_changes {field} {raw!r} is not a valid number"
        ) from exc


class ModelCatalog:
    def __init__(self) -> None:
        self._static = self._load_static()
        self._dynamic: dict[str, ModelInfo] = {}
        self._dynamic_fetched_at: float = 0.0
        self._local: dict[str, ModelInfo] = {}
        self._local_fetched_at: float = 0.0
        # Tool-support probe results, keyed by tret model id. Populated lazily
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
        headless run should have. tret/main.py schedules this at startup as a
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
            active_params_b = m.get("active_params_b")

            # input_price_per_mtok/output_price_per_mtok stay the model's base
            # (yaml) price — never baked against "today" here. `prices_at()`
            # (which `cost_usd` calls on every invocation) resolves whichever
            # `price_changes` entry is actually due, at read time, so a
            # long-running process bills a scheduled change correctly without
            # this catalog ever being reloaded. `ModelInfo.__post_init__`
            # validates the schedule itself (positive prices, strictly
            # ascending dates, each entry's tier matching cost_tier); parsing
            # errors below are just about turning a malformed yaml value into
            # a clear ValueError instead of a raw date/decimal exception.
            price_changes = [
                PriceChange(
                    effective=_parse_price_change_date(m["id"], pc),
                    input_price_per_mtok=_parse_price_change_price(
                        m["id"], pc, "input_price_per_mtok"
                    ),
                    output_price_per_mtok=_parse_price_change_price(
                        m["id"], pc, "output_price_per_mtok"
                    ),
                )
                for pc in m.get("price_changes") or []
            ]
            input_price = Decimal(str(m["input_price_per_mtok"]))
            output_price = Decimal(str(m["output_price_per_mtok"]))

            pricing_tiers = [
                PricingTier(
                    above_prompt_tokens=int(t["above_prompt_tokens"]),
                    input_multiplier=Decimal(str(t["input_multiplier"])),
                    output_multiplier=Decimal(str(t["output_multiplier"])),
                )
                for t in m.get("pricing_tiers") or []
            ]

            cache_read_mult = m.get("cache_read_multiplier")
            cache_write_mult = m.get("cache_write_multiplier")

            info = ModelInfo(
                id=m["id"],
                provider=m["provider"],
                wire_id=m["wire_id"],
                display_name=m["display_name"],
                context_window=m["context_window"],
                input_price_per_mtok=input_price,
                output_price_per_mtok=output_price,
                cost_tier=m["cost_tier"],
                strengths=m.get("strengths", []),
                supports_tools=m.get("supports_tools", True),
                supports_effort=m.get("supports_effort", False),
                curated=True,
                released=str(m["released"]) if m.get("released") else None,
                # Unclassified curated entries fall back to their cost tier's
                # estimated class rather than silently reading as low-energy.
                energy_class=m.get("energy_class") or energy_class_for_tier(m["cost_tier"]),
                energy_wh_per_mtok=Decimal(str(wh_override)) if wh_override else None,
                active_params_b=float(active_params_b) if active_params_b is not None else None,
                cache_read_multiplier=(
                    Decimal(str(cache_read_mult))
                    if cache_read_mult is not None
                    else CACHE_READ_MULTIPLIER
                ),
                cache_write_multiplier=(
                    Decimal(str(cache_write_mult))
                    if cache_write_mult is not None
                    else CACHE_WRITE_MULTIPLIER
                ),
                pricing_tiers=pricing_tiers,
                price_changes=price_changes,
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
            async with open_client(CLASS_CATALOG, timeout=20.0) as client:
                resp = await client.get("https://openrouter.ai/api/v1/models")
            resp.raise_for_status()
            # Parsed inside the try: a 200 that is not JSON (a captive portal or
            # proxy error page, an HTML maintenance notice) raises ValueError, and
            # outside the try that made the whole of GET /api/models fail — the
            # curated catalog with it — instead of degrading to "no dynamic
            # models", which is what "best-effort" has to mean.
            payload = resp.json()
        except (httpx.HTTPError, ValueError, EgressDenied):
            # EgressDenied belongs with the network errors, not above them: an
            # operator who switched the catalog class off asked for exactly this,
            # and it should degrade to "no dynamic models" like an unreachable
            # openrouter.ai does, not fail GET /api/models.
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
            tret_id = f"openrouter/{wire_id}"
            if not wire_id or tret_id in self._static:
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
                    released = datetime.fromtimestamp(int(created), tz=timezone.utc).strftime("%Y-%m")
                except (ValueError, OSError):
                    pass
            # Cheap enrichment: OpenRouter publishes its own cache-read price per
            # entry (`pricing.input_cache_read`), so an uncurated model gets its
            # real ratio instead of silently inheriting the Anthropic-shaped
            # default. Any malformed figure just falls back to that default —
            # this is a nice-to-have, not something worth failing the fetch over.
            cache_kwargs: dict = {}
            cache_read_raw = pricing.get("input_cache_read")
            if cache_read_raw is not None and in_price > 0:
                try:
                    cache_read_price = Decimal(str(cache_read_raw)) * Decimal(1_000_000)
                    if _usable_price(cache_read_price):
                        cache_kwargs["cache_read_multiplier"] = cache_read_price / in_price
                except Exception:  # noqa: BLE001 - enrichment only, never fatal
                    pass
            dynamic[tret_id] = ModelInfo(
                id=tret_id,
                provider="openrouter",
                wire_id=wire_id,
                display_name=m.get("name", wire_id),
                context_window=int(m.get("context_length") or 0),
                input_price_per_mtok=in_price,
                output_price_per_mtok=out_price,
                cost_tier=_tier_from_price(out_price),
                strengths=[],
                supports_tools=True,
                # Same source as supports_tools above: OpenRouter's unified
                # `reasoning.effort` shows up as "reasoning" in this entry's
                # own supported_parameters when the model accepts it.
                supports_effort="reasoning" in supported,
                curated=False,
                released=released,
                # Nobody has classified these by hand: estimate from the price
                # tier (economy→M, standard→L, premium→XL).
                energy_class=energy_class_for_tier(_tier_from_price(out_price)),
                **cache_kwargs,
            )
        self._dynamic = dynamic
        self._dynamic_fetched_at = time.monotonic()

    async def refresh_local(self, *, force: bool = False) -> LocalDiscovery:
        """Discover models from a configured local OpenAI-compat server.

        Best-effort and short-cached (5 min, vs. 24h for OpenRouter): local
        servers get models swapped in and out far more often than a cloud
        catalog, but polling them is also free and local, so a short TTL is
        cheap. An unreachable/misconfigured server yields an empty local
        catalog rather than raising — tret must keep working with zero local
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
            async with open_client(CLASS_LOCAL, timeout=10.0) as client:
                resp = await client.get(
                    f"{base_url}/models",
                    headers={"Authorization": f"Bearer {settings.local_api_key or 'local'}"},
                )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError, EgressDenied) as exc:
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
            tret_id = f"local/{wire_id}"
            context_window = 0
            for key in ("context_length", "max_model_len", "context_window"):
                value = m.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                    context_window = int(value)
                    break
            supports_tools = await self._probe_supports_tools(
                provider, tret_id, wire_id, settings, force=force
            )
            discovered[tret_id] = ModelInfo(
                id=tret_id,
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
        tret_id: str,
        wire_id: str,
        settings,
        *,
        force: bool = False,
    ) -> bool:
        """Cheap forced-tool-call probe, cached per model id.

        Local models routinely advertise an OpenAI-compat surface without
        actually honoring `tools`/`tool_choice`. Since tret's trust model
        (grading, structured extraction, terminal actions) runs entirely
        through tool calls, a model that fails this probe must never reach
        router candidacy — see router_llm/router.py's `supports_tools` filter.

        `force=True` skips the cache *read* (the result is still cached), so a
        model that has since been re-pulled or re-quantized gets a fresh verdict.
        """
        if not settings.local_probe_tools:
            return True
        if not force and tret_id in self._tool_probe_cache:
            return self._tool_probe_cache[tret_id]
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
            ok = _probe_answered(result.payload)
        except Exception:
            ok = False
        self._tool_probe_cache[tret_id] = ok
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
    """Everything tret needs to know about one provider, in one place.

    Adding a provider used to mean editing three parallel provider→something
    maps (the env-key map, the `has_key` special cases, and the if/elif
    construction chain), where forgetting one produced a provider that was
    "configured" but unbuildable, or buildable but never offered. One row here
    now drives all three.

    It also drives the settings endpoint: `tret/api/settings.py` derives its
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
            key,
            referer=settings.openrouter_referer,
            title=settings.openrouter_title,
            provider_prefs=settings.openrouter_provider_prefs,
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
        """Whether this provider can actually be used for a run.

        Egress is part of the answer, not a separate check the callers would each
        have to remember: with `TRET_EGRESS_PROVIDER=off` an Anthropic key is a
        string that cannot reach Anthropic, and a router that offered the model
        anyway would pick it and then fail the run. Filtering here means every
        caller — candidate selection, the models endpoint, the settings UI — sees
        the same, honest availability. (`local` is a separate class and survives
        an air-gapped deployment; see tret/net/policy.py.)
        """
        if effective_mode(self._egress_class(provider)) == MODE_OFF:
            return False
        if provider in self._enabled:
            return self._enabled[provider]
        return bool(self._keys.get(provider))

    @staticmethod
    def _egress_class(provider: str) -> str:
        return CLASS_LOCAL if provider == "local" else CLASS_PROVIDER

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
