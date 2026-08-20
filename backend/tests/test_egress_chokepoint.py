"""One socket door: nothing outside `bench/net/` may open a connection.

The whole point of `bench/net/` is that "can this deployment reach the internet,
and what for?" is answered by reading one directory. That property is worth
exactly as much as the discipline behind it, and discipline is not a thing a
codebase keeps on its own — so it is a test.

Two rules, and the difference between them matters:

* **No connection-opening imports.** `socket`, `requests`, `aiohttp`,
  `urllib.request` and friends have no business anywhere else.
* **No client construction.** `httpx` may be *imported* outside `bench/net/` —
  its exception and timeout types are part of the vocabulary of talking to a
  provider — but `httpx.AsyncClient(...)` and the module-level `httpx.get(...)`
  shortcuts may only be called inside it.

Plus one special case: the Anthropic SDK opens its own connections, so
`AnthropicProvider` must hand it a client built here.

Same standing as `bench/packs/safety.py`: an AST scan catches drift and
accidents, not a determined author. The boundary is the network policy around
the container (docs/hardening.md §9); this keeps the intent legible and stops it
rotting one convenient import at a time.
"""
from __future__ import annotations

import ast
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "bench"
NET = BENCH / "net"

# Modules that exist to open a connection. `urllib.parse` is deliberately absent:
# parsing a URL is not reaching one, and half this codebase does it.
FORBIDDEN_IMPORTS = frozenset(
    {
        "socket",
        "socketserver",
        "ssl",
        "requests",
        "aiohttp",
        "urllib.request",
        "urllib3",
        "http.client",
        "httplib",
        "ftplib",
        "smtplib",
        "telnetlib",
        "websockets",
        "pycurl",
    }
)

# httpx attributes that start a connection.
FORBIDDEN_HTTPX_ATTRS = frozenset(
    {
        "AsyncClient",
        "Client",
        "AsyncHTTPTransport",
        "HTTPTransport",
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
        "request",
        "stream",
    }
)


def _source_files() -> list[Path]:
    return sorted(p for p in BENCH.rglob("*.py") if NET not in p.parents)


def _module_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        base = node.module or ""
        return [base] + [f"{base}.{alias.name}" for alias in node.names]
    return []


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        for name in _module_names(node):
            root = name.split(".")[0]
            if name in FORBIDDEN_IMPORTS or root in FORBIDDEN_IMPORTS:
                found.append(f"{path.name}:{node.lineno} imports {name}")
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "httpx"
                and func.attr in FORBIDDEN_HTTPX_ATTRS
            ):
                found.append(f"{path.name}:{node.lineno} calls httpx.{func.attr}(...)")
    return found


def test_no_module_outside_bench_net_opens_a_connection():
    offenders = [v for path in _source_files() for v in _violations(path)]
    assert not offenders, (
        "These call or import the network directly. Route them through "
        "bench.net.open_client / build_client so they carry an egress class:\n  "
        + "\n  ".join(offenders)
    )


def test_the_scan_would_actually_catch_something(tmp_path):
    """A scan nobody has seen fail is a scan that might match nothing."""
    bad = tmp_path / "bad.py"
    bad.write_text("import httpx\nimport socket\n\nc = httpx.AsyncClient()\n")
    found = _violations(bad)
    assert any("socket" in v for v in found)
    assert any("httpx.AsyncClient" in v for v in found)


def test_the_anthropic_sdk_is_handed_a_bench_client():
    """The one dependency that opens its own sockets takes ours instead.

    `anthropic.AsyncAnthropic()` with no `http_client` builds an httpx client
    inside the SDK, where no policy check can see it — the chokepoint would have
    a hole exactly the width of the provider bench uses most.
    """
    source = (BENCH / "providers" / "anthropic.py").read_text()
    tree = ast.parse(source)
    constructions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "AsyncAnthropic"
    ]
    assert constructions, "AnthropicProvider no longer constructs AsyncAnthropic — update this test"
    for call in constructions:
        keywords = {kw.arg for kw in call.keywords}
        assert "http_client" in keywords, (
            f"anthropic.py:{call.lineno} builds an Anthropic client without http_client=, "
            "so those requests bypass bench/net entirely"
        )
