"""One routed, structured model call: the building block of runs that are not agent loops.

`routed_json_call` asks the model router for a model (never a pinned one: a policy that pins is
refused), sized so its context window holds the prompt plus the answer, calls
`provider.complete_json` with the workspace's own provider keys, validates the answer against the
JSON schema and returns the payload, the usage of EVERY call it made (a failed attempt that was
billed still counts) and the routing decision with its rationale.

One retry: a transient provider error (no status, 408/409/425/429/5xx, a timeout) or an answer that
is not valid JSON for the schema is tried once more (the second prompt names what was wrong with the
first answer). A context overflow, a provider refusal or any other 4xx fails at once with a
`RoutedCallError` whose `kind` says which, carrying the calls already made so their cost can still be
booked. Later stages (the jury, the sealed call) reuse this helper; `tret/engine/direct.py` turns
the usage into `Run` cost, energy and billing.

Tests replace `get_call_services` (or pass `services=`) with fakes; nothing here touches the network
itself.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from tret.providers.base import ProviderError, Usage

log = logging.getLogger("tret.routed_call")

TASK_TYPE = "structured_call"
# Keys of `model_policy` that would make the router skip its choice.
PIN_MODES = ("pinned", "pin", "override")
RETRIABLE_STATUSES = frozenset({408, 409, 425, 429})
_OVERFLOW = re.compile(
    r"context[ _-]?(length|window)|maximum context|too many tokens|prompt is too long|"
    r"exceeds? (the )?(model'?s )?(maximum|max|context)|input is too long|token limit",
    re.I,
)
_REFUSED = re.compile(r"refus|content[ _-]?(policy|filter)|safety|blocked|moderation|not allowed", re.I)

KIND_CONTEXT = "context_overflow"
KIND_REFUSED = "refused"
KIND_INVALID = "invalid_json"
KIND_PROVIDER = "provider"
KIND_UNROUTABLE = "unroutable"
KIND_PINNED = "pinned_policy"


class RoutedCallError(Exception):
    """A routed call that failed. `calls` are the (billed) calls made before it failed."""

    def __init__(self, kind: str, message: str, *, calls: list["CallRecord"] | None = None, decision: Any = None):
        super().__init__(message)
        self.kind = kind
        self.calls = calls or []
        self.decision = decision


@dataclass
class CallRecord:
    model: Any  # ModelInfo
    usage: Usage
    served_by: str | None
    attempt: int
    ok: bool
    error: str | None = None
    estimated: bool = False  # usage guessed (chars / 4), not reported: the call died after it was sent


@dataclass
class RoutedJsonResult:
    payload: dict
    model: str  # catalog id of the model that answered
    served_by: str | None
    usage: Usage  # summed over every call
    decision: Any  # RoutingDecision
    calls: list[CallRecord] = field(default_factory=list)
    purpose: str = ""

    @property
    def attempts(self) -> int:
        return len(self.calls)


@dataclass
class CallServices:
    registry: Any
    router: Any
    catalog: Any


async def get_call_services(db, workspace_id: uuid.UUID | None) -> CallServices:
    """The provider registry (the workspace's own stored keys, like a harness run), the
    router over it and the catalog. Replaced in tests."""
    from tret.providers.catalog import ProviderRegistry, get_catalog
    from tret.router_llm.priors import OutcomePriors
    from tret.router_llm.router import ModelRouter
    from tret.services.credentials import load_db_keys

    catalog = get_catalog()
    registry = ProviderRegistry(await load_db_keys(db, workspace_id))
    return CallServices(registry, ModelRouter(catalog, registry, OutcomePriors()), catalog)


def check_policy(model_policy: dict | None) -> dict:
    """A copy of the policy, or `RoutedCallError(pinned_policy)`: model choice always goes
    through the router."""
    policy = dict(model_policy or {"mode": "auto"})
    if policy.get("mode") in PIN_MODES or policy.get("model"):
        raise RoutedCallError(KIND_PINNED, "this call never uses a pinned model: use an auto policy (objective, allowed, open_weights)")
    policy.setdefault("mode", "auto")
    return policy


def _estimated_usage(est_input_tokens: int) -> Usage:
    """What a call that died after being sent is booked at: the prompt's estimated tokens in, no
    output known."""
    return Usage(input_tokens=max(0, int(est_input_tokens)), output_tokens=0)


def _add(total: Usage, u: Usage) -> None:
    total.input_tokens += u.input_tokens
    total.output_tokens += u.output_tokens
    total.cache_read_tokens += u.cache_read_tokens
    total.cache_write_tokens += u.cache_write_tokens
    if u.reported_cost_usd is not None:
        total.reported_cost_usd = (total.reported_cost_usd or 0) + u.reported_cost_usd


def sum_usage(calls: list[CallRecord]) -> Usage:
    total = Usage()
    for c in calls:
        _add(total, c.usage)
    return total


def classify_provider_error(e: ProviderError) -> tuple[str, bool]:
    """(kind, retriable) for a provider error."""
    text = str(e)
    status = e.status
    if _OVERFLOW.search(text) and (status is None or status in (400, 413, 422)):
        return KIND_CONTEXT, False
    if status == 413:
        return KIND_CONTEXT, False
    if _REFUSED.search(text) and (status is None or 400 <= status < 500):
        return KIND_REFUSED, False
    if status is None or status in RETRIABLE_STATUSES or status >= 500:
        return KIND_PROVIDER, True
    return KIND_PROVIDER, False


def _effective_output(requested: int, model: Any) -> int:
    from tret.engine.harness import _effective_max_output

    return _effective_max_output(requested, model)


async def routed_json_call(
    db,
    *,
    workspace_id: uuid.UUID | None,
    model_policy: dict | None,
    purpose: str,
    system: str,
    prompt: str,
    schema: dict,
    est_input_tokens: int,
    max_output_tokens: int = 4096,
    task_family: str | None = None,
    tool_name: str = "respond",
    timeout: float = 300.0,
    emissions: Any = None,
    services: CallServices | None = None,
    project_id: uuid.UUID | None = None,
    task_description: str | None = None,
    record: dict | None = None,
) -> RoutedJsonResult:
    """Route, call, validate; see the module docstring. `est_input_tokens` is the whole prompt
    (system plus user); the router is asked for a window of at least `est_input_tokens +
    max_output_tokens`. `emissions` (a `_EmissionsContext`) lets the router's own call be
    accounted under the run's emission layers. `task_family` is passed to the router only when
    given. `record`, when given, is filled with `calls` (the live list of `CallRecord`s) and
    `decision` as soon as they exist, so a caller that is cancelled mid-call can still book what
    was sent."""
    from tret.router_llm.router import RoutingUnavailable

    policy = check_policy(model_policy)
    svc = services or await get_call_services(db, workspace_id)
    needed = int(est_input_tokens) + int(max_output_tokens)
    kwargs: dict[str, Any] = {}
    if emissions is not None:
        kwargs.update(
            emissions_workspace_doc=getattr(emissions, "workspace_doc", None),
            emissions_managed_doc=getattr(emissions, "managed_doc", None),
            emissions_at=getattr(emissions, "at", None),
        )
    if task_family:
        kwargs["task_family"] = task_family
    try:
        decision = await svc.router.route(
            model_policy=policy,
            task_type=TASK_TYPE,
            task_shape="extraction",
            task_description=task_description or purpose,
            output_contract="JSON object matching the supplied schema",
            n_documents=1,
            est_input_tokens=int(est_input_tokens),
            min_context_window=needed,
            **kwargs,
        )
    except RoutingUnavailable as e:
        raise RoutedCallError(KIND_UNROUTABLE, str(e)) from e
    model = svc.catalog.get(decision.chosen_model)
    window = getattr(model, "context_window", None)
    out_tokens = _effective_output(max_output_tokens, model)
    if isinstance(window, int) and window > 0 and int(est_input_tokens) + out_tokens > window:
        raise RoutedCallError(
            KIND_CONTEXT,
            f"{purpose}: about {int(est_input_tokens)} input tokens do not fit {model.id} "
            f"(window {window}, output {out_tokens}); no model that fits was available under the policy",
            decision=decision,
        )
    provider = svc.registry.get(model.provider)

    calls: list[CallRecord] = []
    if record is not None:
        record["calls"] = calls
        record["decision"] = decision
    note = ""
    last_error = ""
    for attempt in (1, 2):
        try:
            completion = await provider.complete_json(
                model=model.wire_id,
                system=system,
                prompt=prompt + note,
                schema=schema,
                tool_name=tool_name,
                max_tokens=out_tokens,
                timeout=timeout,
            )
        except ProviderError as e:
            kind, retriable = classify_provider_error(e)
            # No HTTP status = the connection or the stream died after the request was sent: the
            # tokens may have been billed, so estimate them (as the harness does for a turn that
            # dies mid-stream). A status answer (4xx/5xx) was rejected before any work.
            guess = e.usage is None and e.status is None
            calls.append(CallRecord(
                model, e.usage or (_estimated_usage(est_input_tokens) if guess else Usage()), e.served_by,
                attempt, False, str(e), estimated=guess,
            ))
            last_error = str(e)
            if retriable and attempt == 1:
                log.warning("%s: transient provider error, retrying once: %s", purpose, e)
                continue
            raise RoutedCallError(kind, f"{purpose}: {e}", calls=calls, decision=decision) from e
        except asyncio.CancelledError:
            calls.append(CallRecord(
                model, _estimated_usage(est_input_tokens), None, attempt, False, "cancelled", estimated=True,
            ))
            raise
        except (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError) as e:
            calls.append(CallRecord(
                model, _estimated_usage(est_input_tokens), None, attempt, False, f"{type(e).__name__}: {e}",
                estimated=True,
            ))
            last_error = f"{type(e).__name__}: {e}"
            if attempt == 1:
                continue
            raise RoutedCallError(KIND_PROVIDER, f"{purpose}: {last_error}", calls=calls, decision=decision) from e
        payload = completion.payload
        problem = _schema_problem(payload, schema)
        calls.append(
            CallRecord(model, completion.usage, completion.served_by, attempt, problem is None, problem)
        )
        if problem is None:
            return RoutedJsonResult(
                payload=payload,
                model=model.id,
                served_by=completion.served_by,
                usage=sum_usage(calls),
                decision=decision,
                calls=calls,
                purpose=purpose,
            )
        last_error = problem
        note = (
            "\n\nYour previous answer was rejected because it did not match the required JSON "
            f"schema ({problem}). Answer again with a JSON object that matches it exactly."
        )
    raise RoutedCallError(
        KIND_INVALID, f"{purpose}: the model's answer was not valid JSON for the schema twice ({last_error})",
        calls=calls, decision=decision,
    )


def _schema_problem(payload: Any, schema: dict) -> str | None:
    if not isinstance(payload, dict):
        return f"expected a JSON object, got {type(payload).__name__}"
    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as e:
        where = "/".join(str(p) for p in e.absolute_path) or "(root)"
        return f"{where}: {e.message[:200]}"
    except jsonschema.SchemaError as e:  # a bug in the caller's schema, not the model's answer
        raise RoutedCallError(KIND_INVALID, f"the schema itself is invalid: {e.message[:200]}") from e
    return None
