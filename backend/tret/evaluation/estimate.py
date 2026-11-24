"""Cost estimate for an extraction batch, with no model call (`tret disclosure extract --dry-run`).

Reads each report's Markdown from the blob store, counts windows and input tokens (system prompt
included for every window) and prices them at the cheapest and the dearest tool-capable model in the
catalog (output assumed at `OUTPUT_TOKENS_PER_WINDOW`). The router picks the real model, so this is a
range, not a quote.
"""
from __future__ import annotations

from typing import Any

from tret.evaluation import extract as ex
from tret.evaluation.modules import ModuleSet

OUTPUT_TOKENS_PER_WINDOW = 1500


async def estimate_reports(
    reports: list[Any], module_set: ModuleSet, window_tokens: int, policy: dict | None = None
) -> dict:
    from tret.providers.catalog import get_catalog
    from tret.storage import get_store

    store = get_store()
    windows = tokens = missing = 0
    for r in reports:
        try:
            md = (await store.get(r.markdown_key)).decode("utf-8", errors="replace")
        except (FileNotFoundError, TypeError):
            missing += 1
            continue
        w, t = ex.estimate_report_tokens(md, window_tokens, module_set)
        windows += w
        tokens += t
    out = windows * OUTPUT_TOKENS_PER_WINDOW
    costs = []
    for m in get_catalog().all(curated_only=True):
        if not getattr(m, "supports_tools", True):
            continue
        window = getattr(m, "context_window", None) or 0
        if window and window < min(window_tokens, tokens or window_tokens) + OUTPUT_TOKENS_PER_WINDOW:
            continue
        costs.append(float(m.cost_usd(tokens, out)))
    return {
        "reports": len(reports),
        "missing_markdown": missing,
        "windows": windows,
        "input_tokens": tokens,
        "output_tokens": out,
        "cost_usd": {"low": min(costs), "high": max(costs)} if costs else None,
        "policy": policy or {"mode": "auto"},
    }
