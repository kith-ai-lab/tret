"""Local model provider: any OpenAI-compat server (Ollama, LM Studio, vLLM,
llama.cpp's `server` binary) reachable at a configured base URL.

These servers speak the same chat/completions wire format as Kimi/OpenRouter,
so this is a thin subclass of OpenAICompatProvider. The two differences from a
cloud provider:

- auth is usually irrelevant (most local servers ignore the Authorization
  header entirely), so an empty api_key still produces a well-formed
  "Bearer local" header rather than "Bearer " with nothing after it;
- tool-calling support is unreliable across local models/servers, so the
  catalog runs a cheap capability probe (see catalog.py) before letting a
  local model into router candidacy.
"""
from __future__ import annotations

from bench.providers.openai_compat import OpenAICompatProvider


class LocalProvider(OpenAICompatProvider):
    name = "local"

    def __init__(self, base_url: str, api_key: str = "", display_name: str = "Local"):
        self.display_name = display_name
        super().__init__(api_key or "local", base_url=base_url)
