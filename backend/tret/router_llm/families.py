"""Model families: the lab behind a model, as a label.

A family is a constraint the evaluation jury is spread across ("three jurors from three
different labs"), never a model name: nothing here picks, ranks or pins anything. It is built on
the author prefix of the model id (`openai/gpt-5.6-terra` -> `openai`), with a small alias table
for labs that appear under several prefixes (`meta-llama` and `meta` are one lab; `x-ai` is
`xai`; `kimi` is `moonshot`), the OpenRouter-style variant marks (`~anthropic/...`, `:free`
suffixes) and a name heuristic for the direct-provider ids that carry no author at all
(`claude-sonnet-5`). The label is the same whether you pass an id or a `ModelInfo`.

A family is the publisher of the model as released, not of its ancestry: a model derived from
another lab's (nemotron on Llama, an R1 distill of Llama) keeps the PUBLISHER's family (nvidia,
deepseek). That is intended: juror independence is about who trained and serves the model.
"""
from __future__ import annotations

import re
from typing import Any

# Prefix (lower case, after stripping a leading "~") -> family label.
FAMILY_ALIASES: dict[str, str] = {
    "meta-llama": "meta",
    "meta": "meta",
    "facebook": "meta",
    "moonshotai": "moonshot",
    "moonshot": "moonshot",
    "kimi": "moonshot",
    "x-ai": "xai",
    "xai": "xai",
    "mistralai": "mistral",
    "mistral": "mistral",
    "z-ai": "zhipu",
    "zhipu": "zhipu",
    "thudm": "zhipu",
    "qwen": "alibaba",
    "alibaba": "alibaba",
    "deepseek-ai": "deepseek",
    "deepseek": "deepseek",
    "google": "google",
    "gemini": "google",
    "openai": "openai",
    "anthropic": "anthropic",
    "microsoft": "microsoft",
    "amazon": "amazon",
    "nvidia": "nvidia",
    "cohere": "cohere",
    "minimax": "minimax",
    "minimaxai": "minimax",
    "bytedance-seed": "bytedance",
    "bytedance": "bytedance",
    "allenai": "allenai",
}
# Token of the bare model name -> family, for ids with no author prefix. Matched against whole
# tokens (split on - _ . : / and letter/digit boundaries), never substrings: `olmo3` is not `o3`.
_NAME_HINTS: dict[str, str] = {
    "claude": "anthropic",
    "gpt": "openai",
    "o1": "openai",
    "o3": "openai",
    "o4": "openai",
    "gemini": "google",
    "gemma": "google",
    "kimi": "moonshot",
    "llama": "meta",
    "qwen": "alibaba",
    "qwq": "alibaba",
    "mistral": "mistral",
    "mixtral": "mistral",
    "magistral": "mistral",
    "devstral": "mistral",
    "codestral": "mistral",
    "ministral": "mistral",
    "pixtral": "mistral",
    "deepseek": "deepseek",
    "grok": "xai",
    "glm": "zhipu",
    "minimax": "minimax",
    "phi": "microsoft",
    "nova": "amazon",
    "nemotron": "nvidia",
    "command": "cohere",
    "olmo": "allenai",
}
# Tret providers that are one lab: the model is theirs whatever it is called.
_PROVIDER_FAMILY = {"anthropic": "anthropic", "kimi": "moonshot", "openai": "openai", "google": "google"}
UNKNOWN_FAMILY = "unknown"
_ROUTE_PREFIXES = ("openrouter/", "local/")
_SPLIT = re.compile(r"[-_.:/\s]+|(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])")


def _label(prefix: str) -> str:
    p = prefix.strip().lstrip("~").lower()
    return FAMILY_ALIASES.get(p, p)


def _from_name(name: str) -> str | None:
    for token in _SPLIT.split(name.lower()):
        if token in _NAME_HINTS:
            return _NAME_HINTS[token]
    return None


def family_of(model: Any) -> str:
    """The family label of a model id (`openrouter/openai/gpt-5.6-luna`, `anthropic/claude-x`) or
    of a `ModelInfo` (its wire id, then its provider)."""
    if isinstance(model, str):
        mid = model
        provider = None
    else:
        mid = getattr(model, "wire_id", None) or getattr(model, "id", "") or ""
        provider = getattr(model, "provider", None)
    mid = mid.strip()
    for pre in _ROUTE_PREFIXES:
        if mid.startswith(pre):
            mid = mid[len(pre):]
            break
    if "/" in mid:
        return _label(mid.split("/", 1)[0])
    if provider in _PROVIDER_FAMILY:  # a single-lab provider: its models are that lab's
        return _PROVIDER_FAMILY[provider]
    by_name = _from_name(mid)
    if by_name:
        return by_name
    return _label(provider) if provider and provider not in ("openrouter", "local") else UNKNOWN_FAMILY
