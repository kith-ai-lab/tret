"""What bench is allowed to talk to, and how that is switched off.

Egress is not one boolean. bench must reach an LLM provider to do anything at
all, so "no internet" and "no *research* internet" are different deployments and
each needs its own switch. Three classes:

    provider   cloud model calls (anthropic, kimi, openrouter)
    catalog    the OpenRouter model list and provider key validation
    local      a self-hosted model server at BENCH_LOCAL_BASE_URL
    research   web search and page fetch — the only class whose destination is
               chosen by a *model*, from text that may have come from an
               uploaded document

`research` ships **off**. Turning it on is a deliberate act by an operator, and
turning it back off severs the agent's internet without touching the rest of the
product. `BENCH_EGRESS=off` severs `provider`, `catalog` and `research`, which is
the air-gapped deployment: local models only.

Why `local` is its own class, exempt from the master switch: a request to a model
server on your own machine never leaves the deployment, and an air-gapped bench
that could not reach it would be an air-gapped bench that does nothing. bench
cannot take that on faith, though — `BENCH_LOCAL_BASE_URL` is a string, and it
could name a public host. So with the master switch off the `local` class flips
to *require* a private address: local inference keeps working, and a "local" URL
that is secretly on the internet is refused. The exemption is checked, not
assumed.

The mode lattice is `off < replay < on`, and every rule here **narrows**. The
master switch narrows the class switch, and a runtime override (the settings
API, held in memory) narrows both — never widens. That direction matters: if an
admin session could widen egress, the kill switch would only be as strong as the
weakest admin account, and it is supposed to be as strong as the environment the
process was started in.

What this module is NOT: a security boundary. An app-level allowlist is enforced
by code running inside the process it constrains. The boundary is the network
policy around the container — see docs/hardening.md §9.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from bench.config import get_settings

log = logging.getLogger("bench.net")

CLASS_PROVIDER = "provider"
CLASS_CATALOG = "catalog"
CLASS_LOCAL = "local"
CLASS_RESEARCH = "research"
EGRESS_CLASSES = (CLASS_PROVIDER, CLASS_CATALOG, CLASS_LOCAL, CLASS_RESEARCH)

MODE_OFF = "off"
MODE_REPLAY = "replay"
MODE_ON = "on"
MODES = (MODE_OFF, MODE_REPLAY, MODE_ON)

VERIFY_NONE = "none"
VERIFY_PUBLIC = "public"
VERIFY_PRIVATE = "private"

# Narrowness rank. Lower is narrower; `narrower()` is the only way modes combine.
_RANK = {MODE_OFF: 0, MODE_REPLAY: 1, MODE_ON: 2}

# Hosts each non-research class may reach, as a literal rather than a value read
# out of `bench/providers/`: providers import this module, so importing them back
# would be circular. `tests/test_egress_policy.py` asserts every provider base
# URL in the codebase is covered here, which is the property the duplication
# would otherwise put at risk.
PROVIDER_HOSTS = frozenset({"api.anthropic.com", "api.moonshot.ai", "openrouter.ai"})
CATALOG_HOSTS = frozenset({"openrouter.ai"})


class EgressDenied(RuntimeError):
    """A request policy refused. Carries the reason code, for the audit row."""

    def __init__(self, reason: str, url: str, egress_class: str, detail: str = ""):
        self.reason = reason
        self.url = url
        self.egress_class = egress_class
        self.detail = detail
        super().__init__(detail or f"{reason}: {url} ({egress_class})")


@dataclass(frozen=True)
class ClassPolicy:
    """The resolved rules for one destination class."""

    name: str
    mode: str
    allow_hosts: frozenset[str]  # empty = any host the other rules permit
    allow_http: bool  # may use plain http as well as https
    standard_ports_only: bool  # 443/80 only — a web page never lives elsewhere
    # Whether the host's resolved addresses are checked, and against what:
    #
    #   none     don't resolve. For provider/catalog, where `allow_hosts` is a
    #            fixed literal (api.anthropic.com, openrouter.ai) that already
    #            pins the destination — resolving would add a lookup to every
    #            model call and rule out nothing the allowlist has not.
    #   public   every resolved address must be public. The SSRF rule, for the
    #            one class whose URL comes from a model.
    #   private  every resolved address must be *non*-public. Only `local`, and
    #            only when the master switch is off: it is what makes "air-gapped,
    #            local models only" a checked claim rather than a naming
    #            convention.
    verify_addresses: str
    max_bytes: int
    timeout_seconds: float

    @property
    def enabled(self) -> bool:
        return self.mode != MODE_OFF


def narrower(*modes: str) -> str:
    """The narrowest of several modes. Unknown strings read as `off`."""
    return min(modes, key=lambda m: _RANK.get(m, 0))


def _normalize(mode: str) -> str:
    candidate = (mode or "").strip().lower()
    return candidate if candidate in MODES else MODE_OFF


# Runtime narrowing, set through the settings API. In-memory and per-process:
# it is a kill switch an operator can reach without a redeploy, not durable
# configuration. A restart returns to what the environment says, which is the
# behaviour that makes the environment the real ceiling.
_runtime_overrides: dict[str, str] = {}


def set_runtime_override(egress_class: str, mode: str) -> str:
    """Narrow one class at runtime. Returns the mode now in force.

    Asking for a *wider* mode than the environment permits is not an error and
    not a partial success: the override is recorded and the environment still
    wins, so the caller is told the effective mode rather than the requested one.
    """
    if egress_class not in EGRESS_CLASSES:
        raise ValueError(f"unknown egress class: {egress_class}")
    _runtime_overrides[egress_class] = _normalize(mode)
    return effective_mode(egress_class)


def clear_runtime_override(egress_class: str) -> str:
    _runtime_overrides.pop(egress_class, None)
    return effective_mode(egress_class)


def clear_all_runtime_overrides() -> None:
    _runtime_overrides.clear()


def _configured_mode(egress_class: str, settings) -> str:
    if egress_class == CLASS_PROVIDER:
        return _normalize(settings.egress_provider)
    if egress_class == CLASS_CATALOG:
        return _normalize(settings.egress_catalog)
    if egress_class == CLASS_LOCAL:
        return _normalize(settings.egress_local)
    return _normalize(settings.egress_research)


def master_is_off(settings=None) -> bool:
    return _normalize((settings or get_settings()).egress) == MODE_OFF


def effective_mode(egress_class: str, settings=None) -> str:
    """The mode actually in force: master ∧ class ∧ runtime override.

    `local` is not narrowed by the master switch — see the module docstring for
    why, and `policy_for` for the check that keeps the exemption honest.
    """
    s = settings or get_settings()
    mode = _configured_mode(egress_class, s)
    if egress_class != CLASS_LOCAL:
        mode = narrower(_normalize(s.egress), mode)
    if egress_class in _runtime_overrides:
        mode = narrower(mode, _runtime_overrides[egress_class])
    # `replay` is a research concept (serve from the snapshot cache, fetch
    # nothing). For the other classes there is nothing to replay, and treating
    # it as "on" would quietly widen; it reads as off.
    if mode == MODE_REPLAY and egress_class != CLASS_RESEARCH:
        return MODE_OFF
    return mode


def _hosts_from_urls(*urls: str) -> set[str]:
    out = set()
    for url in urls:
        host = urlsplit(url or "").hostname
        if host:
            out.add(host.lower())
    return out


def research_allow_hosts(settings=None) -> frozenset[str]:
    """Hosts the research class may reach.

    Empty means *any public host*, which is what a general web search needs and
    is stated plainly in .env.example rather than hidden: an allowlist covering
    the open web is not an allowlist. Set the variable to turn research into a
    genuine allowlist (entries match the host itself and its subdomains).

    Whatever the operator sets, the configured search endpoint is added — a
    search backend the search tool cannot reach is a misconfiguration with a
    confusing symptom, and the operator already chose that host by naming it.
    """
    s = settings or get_settings()
    configured = {
        h.strip().lower().lstrip(".")
        for h in (s.egress_research_allow_hosts or "").split(",")
        if h.strip()
    }
    if not configured:
        return frozenset()
    return frozenset(configured | _hosts_from_urls(s.searxng_base_url, _brave_endpoint(s)))


def _brave_endpoint(settings) -> str:
    return "https://api.search.brave.com" if (settings.search_provider or "").strip() == "brave" else ""


def policy_for(egress_class: str, settings=None) -> ClassPolicy:
    s = settings or get_settings()
    mode = effective_mode(egress_class, s)
    if egress_class == CLASS_RESEARCH:
        return ClassPolicy(
            name=egress_class,
            mode=mode,
            allow_hosts=research_allow_hosts(s),
            allow_http=False,
            standard_ports_only=True,
            verify_addresses=VERIFY_PUBLIC,
            max_bytes=int(s.egress_research_max_bytes),
            timeout_seconds=float(s.egress_research_timeout_seconds),
        )
    if egress_class == CLASS_LOCAL:
        return ClassPolicy(
            name=egress_class,
            mode=mode,
            allow_hosts=frozenset(_hosts_from_urls(s.local_base_url)),
            allow_http=True,  # a local server on a LAN address, usually without TLS
            standard_ports_only=False,  # 11434 (Ollama), 1234 (LM Studio), 8000 (vLLM)
            verify_addresses=VERIFY_PRIVATE if master_is_off(s) else VERIFY_NONE,
            max_bytes=0,
            timeout_seconds=300.0,
        )
    if egress_class == CLASS_CATALOG:
        return ClassPolicy(
            name=egress_class,
            mode=mode,
            allow_hosts=CATALOG_HOSTS,
            allow_http=False,
            standard_ports_only=True,
            verify_addresses=VERIFY_NONE,
            max_bytes=0,
            timeout_seconds=20.0,
        )
    return ClassPolicy(
        name=egress_class,
        mode=mode,
        allow_hosts=PROVIDER_HOSTS,
        allow_http=False,
        standard_ports_only=True,
        verify_addresses=VERIFY_NONE,
        max_bytes=0,
        timeout_seconds=300.0,
    )


def host_allowed(host: str, policy: ClassPolicy) -> bool:
    """Exact host, or a subdomain of an allowlisted host. Empty list = any."""
    if not policy.allow_hosts:
        return True
    candidate = (host or "").lower().rstrip(".")
    return any(
        candidate == allowed or candidate.endswith("." + allowed) for allowed in policy.allow_hosts
    )


def egress_status(settings=None) -> dict:
    """What is switched on right now, for the settings API and the boot log."""
    s = settings or get_settings()
    return {
        "master": _normalize(s.egress),
        "proxy": bool((s.egress_proxy or "").strip()),
        "classes": {
            name: {
                "mode": effective_mode(name, s),
                "configured": _configured_mode(name, s),
                "runtime_override": _runtime_overrides.get(name),
                "allow_hosts": sorted(policy_for(name, s).allow_hosts),
            }
            for name in EGRESS_CLASSES
        },
    }


def log_egress_at_boot(settings=None, logger=None) -> None:
    """Say what is dark, once, at startup.

    A deployment with egress off looks broken in exactly the same way as a
    deployment with a bad API key — empty model list, failing runs. The
    difference is one line in the log, so it goes in the first lines of it.
    """
    s = settings or get_settings()
    out = logger or log
    for name in EGRESS_CLASSES:
        mode = effective_mode(name, s)
        if mode == MODE_ON:
            continue
        out.warning(
            "egress class %r is %s: %s",
            name,
            mode,
            {
                CLASS_PROVIDER: "no model calls will reach a cloud provider",
                CLASS_CATALOG: "the OpenRouter catalog and key validation are unavailable",
                CLASS_LOCAL: "the configured local model server is unreachable",
                CLASS_RESEARCH: "web_search and fetch_url are withheld from every run",
            }[name],
        )
