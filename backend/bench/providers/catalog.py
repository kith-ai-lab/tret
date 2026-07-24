"""Unified model catalog + provider registry.

The catalog is the single source the router candidates, UI pickers, and cost
accounting all read. Static curated entries (models.yaml) always win; the
optional dynamic OpenRouter fetch adds clearly-marked "uncurated" entries.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import httpx
import yaml

from bench.config import get_settings
from bench.providers.anthropic import AnthropicProvider
from bench.providers.base import Provider
from bench.providers.openai_compat import KimiProvider, OpenRouterProvider

_MODELS_YAML = Path(__file__).parent / "models.yaml"
_OPENROUTER_CACHE_TTL = 60 * 60 * 24  # 24h


@dataclass
class ModelInfo:
    id: str  # "anthropic/claude-sonnet-5" — bench-wide id, provider-prefixed
    provider: str  # anthropic | kimi | openrouter
    wire_id: str  # what the provider API receives
    display_name: str
    context_window: int
    input_price_per_mtok: Decimal
    output_price_per_mtok: Decimal
    cost_tier: str  # economy | standard | premium
    strengths: list[str] = field(default_factory=list)
    supports_tools: bool = True
    curated: bool = True
    released: str | None = None  # YYYY-MM; feeds the router's prefer-newer rule

    def cost_usd(self, input_tokens: int, output_tokens: int) -> Decimal:
        return (
            self.input_price_per_mtok * input_tokens
            + self.output_price_per_mtok * output_tokens
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
        }


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

    @staticmethod
    def _load_static() -> dict[str, ModelInfo]:
        raw = yaml.safe_load(_MODELS_YAML.read_text())
        out: dict[str, ModelInfo] = {}
        for m in raw["models"]:
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
        except httpx.HTTPError:
            return  # dynamic catalog is best-effort
        dynamic: dict[str, ModelInfo] = {}
        for m in resp.json().get("data", []):
            wire_id = m.get("id", "")
            bench_id = f"openrouter/{wire_id}"
            if not wire_id or bench_id in self._static:
                continue
            supported = m.get("supported_parameters") or []
            if "tools" not in supported:
                continue
            pricing = m.get("pricing", {})
            try:
                in_price = Decimal(str(pricing.get("prompt", "0"))) * Decimal(1_000_000)
                out_price = Decimal(str(pricing.get("completion", "0"))) * Decimal(1_000_000)
            except Exception:
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
            )
        self._dynamic = dynamic
        self._dynamic_fetched_at = time.monotonic()

    def get(self, model_id: str) -> ModelInfo | None:
        return self._static.get(model_id) or self._dynamic.get(model_id)

    def all(self, curated_only: bool = False) -> list[ModelInfo]:
        items = list(self._static.values())
        if not curated_only:
            items += list(self._dynamic.values())
        return items


class ProviderRegistry:
    """Constructs providers from configured keys. Env keys win over DB keys."""

    def __init__(self, db_keys: dict[str, str] | None = None):
        settings = get_settings()
        db_keys = db_keys or {}
        self._keys = {
            "anthropic": settings.anthropic_api_key or db_keys.get("anthropic", ""),
            "kimi": settings.moonshot_api_key or db_keys.get("kimi", ""),
            "openrouter": settings.openrouter_api_key or db_keys.get("openrouter", ""),
        }
        self._instances: dict[str, Provider] = {}

    def has_key(self, provider: str) -> bool:
        return bool(self._keys.get(provider))

    def available_providers(self) -> list[str]:
        return [p for p, k in self._keys.items() if k]

    def get(self, provider: str) -> Provider:
        if provider not in self._instances:
            key = self._keys.get(provider, "")
            if not key:
                raise KeyError(f"No API key configured for provider '{provider}'")
            settings = get_settings()
            if provider == "anthropic":
                self._instances[provider] = AnthropicProvider(key)
            elif provider == "kimi":
                self._instances[provider] = KimiProvider(key)
            elif provider == "openrouter":
                self._instances[provider] = OpenRouterProvider(
                    key, referer=settings.openrouter_referer, title=settings.openrouter_title
                )
            else:
                raise KeyError(f"Unknown provider '{provider}'")
        return self._instances[provider]


_catalog: ModelCatalog | None = None


def get_catalog() -> ModelCatalog:
    global _catalog
    if _catalog is None:
        _catalog = ModelCatalog()
    return _catalog
