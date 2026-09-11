"""Prose grounding: the same discipline `validation.py`'s cited-values
cross-check holds structured output to, applied to chat/freeform text.

A chat/freeform task's answer *is* its prose — there is no schema and no
`cited_values` array for `validate_cited_values` to check numbers against. A
model can therefore say "the retrieved record shows score 58" in a run where
every `lookup_dataset` call came back empty, and nothing in the loop notices.
This module gives the engine (`engine/harness.py`) a number-level version of
the same check: every number in a reply must trace to something a tool
returned or something the user wrote, or it gets flagged.

It works in two passes. `extract_numbers` finds numeric tokens in one piece of
text and normalises them so `71`, `71.0` and `71.00` (or `1,234` and `1234`)
compare equal. `evidence_numbers` unions that extraction over everything the
model actually saw this run — tool results (minus their own echoed call
arguments — see its docstring), the system prompt, the user's own words, and
prior *user* turns — while carefully excluding the engine's own injected
nudges (they are not evidence; they are the engine talking to the model) and
every assistant-role message, from this run or an earlier one, including the
reply being checked — a rejected draft's own fabricated numbers must not
count as evidence for its rewrite, and neither must a prior run's, which may
itself have shipped one unresolved. `unsupported_numbers` is the diff: a
reply number not in evidence outright still clears if it is evidence rounded,
a percent/fraction of evidence, or arithmetic over other supported reply
numbers (see its docstring for all four rules) — and the whole check is
skipped, not merely passed, on a run with nothing retrieved to contradict
(`run_has_retrieval_evidence`).
"""
from __future__ import annotations

import itertools
import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from tret.providers.base import Msg
from tret.services.transcript import ENGINE_NUDGE_KEY

# The model gets two rewrites after a grounding failure; the third failing
# reply ships as-is, flagged unresolved, rather than being nudged again.
# Mirrors `RunContext.max_repair_attempts`'s shape (engine/tools.py,
# engine/harness.py `_record`): the count of *failed checks* is compared
# against this, not the count of nudges sent — the attempt that meets the
# ceiling is kept rather than retried, so at most `GROUNDING_MAX_REPAIRS - 1`
# nudges are ever sent.
GROUNDING_MAX_REPAIRS = 3

# Two independent lookbehinds, not one combined `[\w.\-]` class, is what lets
# a number preceded by a *digit-only* hyphen through while still excluding a
# letter-coded one:
#   `(?<![\w.])`      — not glued onto a word character or a decimal point.
#   `(?<![A-Za-z]-)`  — not immediately after "<letter>-", which is what a
#                        hazard code's numeric suffix always looks like
#                        (`S-011`, `R-VALLEY`, `COVID-19`) and a numeric range
#                        never does.
# Together these let `2019-2021` and `40-58` yield both operands (the second
# number is preceded by "<digit>-", which neither lookbehind excludes) while
# still refusing a code's suffix (preceded by "<letter>-"). Deliberately does
# not match a leading minus sign either way: a number preceded by `-` with no
# letter before that still fails the first lookbehind if anything else
# preceded it, and where nothing did there simply is no sign to speak of in
# this module's terms (see `_normalize`'s own note on `-0`) — this was already
# true before the split and stays true after it.
#
# The number itself is two alternatives, tried in that order:
#   `dec` — a decimal (`\.\d+` required) with an optional trailing UNIT
#           LETTER *not* captured into the value, so `1.5C` extracts `1.5`
#           rather than failing to match at all (the old single-branch regex
#           would backtrack off the decimal point entirely rather than accept
#           a glued unit, silently dropping the fractional part). The choice
#           to allow this only for decimals, not bare integers, is deliberate
#           and simpler than trying to special-case which trailing letters are
#           "units": a bare integer glued to a letter (`27001a`) is far more
#           likely to be a code than a cited figure with a suffix, so it stays
#           excluded exactly as before.
#   `int` — a plain integer/percent, unchanged from before: no trailing letter
#           tolerated at all.
# `(?![\w])` still closes off both — a trailing digit or underscore right
# after the consumed unit letter (or after a bare integer) still isn't a
# match.
_NUMBER_RE = re.compile(
    r"(?<![\w.])(?<![A-Za-z]-)"
    r"(?:(?P<dec>\d[\d,]*\.\d+%?)[A-Za-z]?(?![\w])|(?P<int>\d[\d,]*%?)(?![\w]))"
)

# `\d{4}-\d{2}-\d{2}` — an ISO date. A date is not a claimed figure ("filed on
# 2026-09-11" is not citing the numbers 2026, 9 and 11 as data), so any
# `_NUMBER_RE` match that falls inside a span this pattern covers is dropped
# in `_iter_number_tokens` rather than treated as evidence or as something a
# reply must ground. Deliberately narrow (exactly this shape) rather than
# trying to recognise dates in general — a real value that merely resembles
# part of a date (a four-digit year on its own, `2026` with no `-09-11`
# attached) is still a number and still matches.
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Below this, a bare integer (no decimal point) is exempt — "3 tools", "we
# checked 2 datasets". Small counts like these are ordinary English, not the
# kind of figure the trust doctrine cares about, and treating every one as a
# citation would flag prose no one would call fabricated.
_EXEMPT_BELOW = 10


def _normalize(raw: str) -> str:
    """Canonical form of a matched numeric token, `%` and thousands commas
    stripped, so `71`, `71.0`, `71.00` and `1,234` / `1234` compare equal.

    `Decimal.normalize()` strips trailing zeros (and can switch to exponent
    form, e.g. `100` -> `1E+2`); `format(..., 'f')` puts it back in fixed
    notation. Zero is special-cased because `Decimal('0').normalize()` can
    come back as `Decimal('-0')` depending on the input's own sign bit
    (irrelevant here since the regex never captures a leading `-`, but this
    keeps the function correct on its own terms rather than by accident of
    what callers happen to pass it).
    """
    value = Decimal(raw.rstrip("%").replace(",", ""))
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _decimal_places(raw: str) -> int:
    """How many digits follow the decimal point in a token's raw surface
    form, or 0 for an integer — used both to tell the `dec`/`int` regex
    branches apart after the fact and, in `unsupported_numbers`, as the
    precision a rounding comparison must match to.
    """
    core = raw.split("%", 1)[0]
    if "." not in core:
        return 0
    return len(core.split(".", 1)[1])


def _iter_number_tokens(text: str) -> list[tuple[str, str]]:
    """`(raw surface form, normalized value)` for every non-exempt numeric
    token in `text`, in the order they appear.

    Three exemptions, applied per occurrence (the same figure elsewhere in
    the text, not as a list ordinal, a date, or a small bare count, still
    counts):

    * a markdown list ordinal at the start of its line (`1. `, `2) `) — the
      list numbering itself is not a claimed figure;
    * any piece of an ISO date (`_ISO_DATE_RE`) — `2026-09-11` is a date, not
      three cited numbers;
    * a bare integer under `_EXEMPT_BELOW` with no decimal point — ordinary
      small counts in prose.
    """
    if not text:
        return []
    date_spans = [m.span() for m in _ISO_DATE_RE.finditer(text)]
    out: list[tuple[str, str]] = []
    for m in _NUMBER_RE.finditer(text):
        if any(m.start() < de and m.end() > ds for ds, de in date_spans):
            continue  # inside an ISO date — not a claimed figure at all
        # The `dec` branch may have consumed a trailing unit letter (`1.5C`)
        # that is not part of the value — group(0) would include it, so the
        # raw surface form comes from whichever named branch actually
        # matched, never from the whole match.
        raw = m.group("dec") or m.group("int")
        # `[\d,]*` (deliberately, to accept `1,234`) also happily eats a comma
        # that is really just punctuation after the number ("score 58, rating
        # moderate") rather than a thousands separator — there is no digit
        # after it either way, so a trailing comma is never actually part of
        # the value and is stripped before it becomes this token's surface
        # form.
        raw = raw.rstrip(",")
        line_start = text.rfind("\n", 0, m.start()) + 1
        prefix = text[line_start : m.start()]
        if prefix.strip() == "":
            after = text[m.end() : m.end() + 2]
            if after[:1] in (".", ")") and (len(after) < 2 or after[1].isspace()):
                continue  # markdown list ordinal, e.g. "1. " / "2) "
        try:
            normalized = _normalize(raw)
        except InvalidOperation:  # pragma: no cover - regex shouldn't allow this
            continue
        if "." not in raw and "%" not in raw:
            try:
                if abs(int(normalized)) < _EXEMPT_BELOW:
                    continue
            except ValueError:  # pragma: no cover - normalized is always int-ish here
                pass
        out.append((raw, normalized))
    return out


def extract_numbers(text: str) -> set[str]:
    """The set of normalized numeric values present in `text`."""
    return {normalized for _raw, normalized in _iter_number_tokens(text)}


def _call_arguments_by_id(messages: list[Msg]) -> dict[str, str]:
    """`tool_call_id -> json.dumps(arguments)` for every tool call any
    assistant message in `messages` made, so a matching tool-result message's
    own evidence can be checked against exactly what it was asked for.
    """
    out: dict[str, str] = {}
    for msg in messages:
        if msg.role != "assistant":
            continue
        for call in msg.tool_calls:
            out[call.id] = json.dumps(call.arguments, default=str)
    return out


def evidence_numbers(
    *,
    system: str,
    messages: list[Msg],
    task_input: dict,
    retrieved: list[dict] | None = None,
) -> set[str]:
    """Every number the model could honestly have gotten from this run.

    The union of: the system prompt; every tool-result message, MINUS
    whatever numbers that same message's own call arguments echo back (see
    below — errors are otherwise still included, since a tool's own "no rows"
    message can carry numbers a real row actually contains, e.g. through
    `retrieved`); every user-role message except the engine's own nudges
    (`meta[ENGINE_NUDGE_KEY]`) and the wire-only budget line
    (`meta["budget_line"]`, which should never reach `messages` — guarded
    anyway since this is evidence, not the wire); the task's own `message`;
    only the user-role entries of `task_input["_history"]`; and `retrieved`
    (the registry `engine/validation.py`'s own cited-values cross-check
    trusts — `ctx.retrieved_values`, a list of `{..., "value": ...}` dicts).

    A tool result that merely echoes the model's own call arguments back is
    not evidence for those numbers, no matter how the tool phrases it:
    `lookup_dataset`'s "No rows in 'hazard_scores' match {"score": 58}" would
    otherwise launder any number the model cared to ask for into "something
    this run retrieved", the same fabrication the check exists to catch, just
    routed through a tool call instead of stated directly. Each tool-result
    message is matched to the call it answers by `tool_call_id`
    (`_call_arguments_by_id`, above), and that call's own argument numbers are
    subtracted from the result's numbers before the union — a filter value a
    matched row genuinely *contains* is still evidence, but only via
    `retrieved`, which only ever holds values a row actually returned
    (`ctx.retrieved_values` — see `engine/tools.py`'s `lookup_dataset`), never
    a value merely asked for.

    No assistant-role message in `messages` counts as evidence, not even an
    earlier turn of this same run: after a grounding nudge, the rejected
    draft that triggered it is still sitting right there in the transcript,
    and if its own fabricated numbers counted as evidence the rewrite could
    just repeat them and pass. Previous *conversation* turns are still
    evidence — they arrive through the user-role entries of
    `task_input["_history"]`, which this function keeps reading regardless of
    what `messages` holds. The assistant-role entries there are NOT evidence,
    for the same reason as above: `task_input["_history"]` is built from this
    project's own prior *runs*, and a prior run's assistant reply can itself
    have shipped `unresolved` — still carrying a number nothing ever
    supported — precisely because the grounding budget was exhausted rather
    than the number being retracted. Counting it as evidence for the next
    turn would let a fabrication launder itself across turns instead of
    across rewrites.
    """
    numbers: set[str] = set(extract_numbers(system or ""))
    call_args = _call_arguments_by_id(messages)

    for msg in messages:
        meta = msg.meta or {}
        if msg.role == "tool":
            result_numbers = extract_numbers(msg.content or "")
            call_json = call_args.get(msg.tool_call_id)
            if call_json:
                result_numbers -= extract_numbers(call_json)
            numbers |= result_numbers
        elif msg.role == "user":
            if meta.get(ENGINE_NUDGE_KEY) is not None or meta.get("budget_line"):
                continue
            numbers |= extract_numbers(msg.content or "")
        # assistant-role messages are never evidence — see docstring.

    numbers |= extract_numbers(task_input.get("message") or "")
    for entry in task_input.get("_history") or []:
        if isinstance(entry, dict) and entry.get("role") == "user":
            numbers |= extract_numbers(entry.get("content") or "")
    for entry in retrieved or []:
        numbers |= extract_numbers(str(entry.get("value", "")))

    return numbers


def run_has_retrieval_evidence(messages: list[Msg], retrieved: list[dict] | None) -> bool:
    """True once this run has something a reply's numbers could honestly
    trace to: either `retrieved` (`ctx.retrieved_values`) already holds a
    value, or some tool actually ran this run (a `role="tool"` message
    exists — success or error alike, since even a "no rows" answer is proof a
    lookup was attempted).

    False only for a run that never touched a tool at all: a pure-knowledge
    chat answer has nothing retrieved to contradict, so running the check
    against an empty evidence set would flag ordinary prose no one asked to
    be grounded. The caller (`engine/harness.py`) records `checked: False,
    status: "skipped"` for this case rather than either skipping the field
    entirely (indistinguishable from a run that predates this check) or
    reporting `"clean"` (which would claim a check that never happened).
    """
    if retrieved:
        return True
    return any(m.role == "tool" for m in messages)


# Half a unit in a token's last decimal place — the tolerance `unsupported_
# numbers`'s arithmetic rule (d) allows between a reply token and a sum,
# difference, or mean derived from other reply tokens, so that "which are
# themselves supported" arithmetic on already-rounded figures does not fail
# over the rounding itself (58 + 22 = 80 must still clear 79.6 stated as 80).
_ARITHMETIC_TOLERANCE_UNITS = Decimal("0.5")


def _as_decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value)
    except InvalidOperation:  # pragma: no cover - callers only pass normalized/evidence forms
        return None


def _rounds_to_some_evidence(val: Decimal, places: int, evidence_decimals: list[Decimal]) -> bool:
    """Rule (b): some evidence value, rounded to `places` decimal places
    (0 for an integer reply token), equals `val` — a reply that rounds a
    retrieved figure for readability (`58` for a retrieved `58.37`) is not
    fabricating, it is summarizing.
    """
    quant = Decimal(1).scaleb(-places) if places else Decimal(1)
    for ev in evidence_decimals:
        try:
            if ev.quantize(quant, rounding=ROUND_HALF_UP) == val:
                return True
        except InvalidOperation:  # pragma: no cover - fixed, small quant exponents
            continue
    return False


def _percent_or_fraction_match(val: Decimal, is_percent: bool, evidence_decimals: list[Decimal]) -> bool:
    """Rule (c): a `%`-suffixed reply token matches evidence holding the
    equivalent fraction (`71%` clears an evidence `0.71`), and a bare reply
    token matches evidence holding the equivalent fraction the other
    direction (`71` clears an evidence `0.71`) — restricted to evidence
    strictly between 0 and 1 so an unrelated small whole number (evidence
    `71` itself) can never be mistaken for a fraction of something.
    """
    if is_percent:
        target = val / Decimal(100)
        return any(ev == target for ev in evidence_decimals)
    return any(0 < ev < 1 and ev * 100 == val for ev in evidence_decimals)


def _supported_by_evidence(raw: str, normalized: str, evidence: set[str], evidence_decimals: list[Decimal]) -> bool:
    """Rules (a)-(c): exact match, rounding, and percent/fraction — every way
    a reply token can be honestly derived from a single evidence value.
    Arithmetic (d), which draws on *other* reply tokens rather than evidence
    directly, is applied separately in `unsupported_numbers`.
    """
    if normalized in evidence:
        return True
    val = _as_decimal(normalized)
    if val is None:  # pragma: no cover - normalized is always Decimal-parseable
        return False
    if _rounds_to_some_evidence(val, _decimal_places(raw), evidence_decimals):
        return True
    return _percent_or_fraction_match(val, "%" in raw, evidence_decimals)


def unsupported_numbers(reply: str, evidence: set[str]) -> list[str]:
    """Numbers `reply` states that `evidence` does not support, by any of:

    (a) an exact normalized match (`58` against evidence `58`);
    (b) rounding — see `_rounds_to_some_evidence`;
    (c) percent/fraction — see `_percent_or_fraction_match`;
    (d) arithmetic — the token equals the sum, difference, or mean of two
        OTHER reply tokens that are themselves supported by (a)-(c), within
        `_ARITHMETIC_TOLERANCE_UNITS` of the *claimed* token's own last
        decimal place. A reply deriving "the combined score is 80" from two
        retrieved figures it also states is reasoning over evidence, not
        inventing a new one — but this deliberately does not chain (an
        arithmetic-supported token can never itself support a further
        arithmetic token), so a run cannot bootstrap an unbounded tower of
        "derived" figures from a single retrieved seed.

    Returned as they appeared in `reply` (original surface form, so a caller
    can quote them back at the model verbatim), deduplicated, in order of
    first appearance.
    """
    tokens = _iter_number_tokens(reply)
    if not tokens:
        return []

    evidence_decimals = [d for d in (_as_decimal(v) for v in evidence) if d is not None]

    # One raw form per distinct value — (b)/(c)/(d) only need the value once,
    # and (d)'s "other reply tokens" pool is naturally deduplicated this way
    # too (repeating a supported figure twice does not buy it extra weight).
    unique: dict[str, str] = {}
    for raw, normalized in tokens:
        unique.setdefault(normalized, raw)

    abc_supported = {
        normalized: _supported_by_evidence(raw, normalized, evidence, evidence_decimals)
        for normalized, raw in unique.items()
    }
    others = {n: _as_decimal(n) for n, ok in abc_supported.items() if ok}

    arithmetic_supported: set[str] = set()
    for normalized, raw in unique.items():
        if abc_supported[normalized]:
            continue
        val = _as_decimal(normalized)
        if val is None:  # pragma: no cover - normalized is always Decimal-parseable
            continue
        tolerance = _ARITHMETIC_TOLERANCE_UNITS.scaleb(-_decimal_places(raw))
        for (_yn, yv), (_zn, zv) in itertools.combinations_with_replacement(others.items(), 2):
            if (
                abs(val - (yv + zv)) <= tolerance
                or abs(val - abs(yv - zv)) <= tolerance
                or abs(val - (yv + zv) / 2) <= tolerance
            ):
                arithmetic_supported.add(normalized)
                break

    seen: set[str] = set()
    out: list[str] = []
    for raw, normalized in tokens:
        if normalized in seen:
            continue
        seen.add(normalized)
        if abc_supported[normalized] or normalized in arithmetic_supported:
            continue
        out.append(raw)
    return out


def grounding_nudge_message(unsupported: list[str]) -> str:
    """The message appended to the conversation when a reply cites numbers
    nothing in the run backs up — same shape as the other structural nudges
    in `engine/harness.py` (`NUDGE_EMPTY_REPLY`, `NUDGE_TERMINAL`): plain
    instruction text, with the evidence for *why* carried on `meta` instead
    of parsed back out of the wording (see `services/transcript.py`'s own
    note on textual vs. structural markers).
    """
    return (
        "Grounding check: these figures appear in none of the data retrieved in this run "
        f"and nowhere in the conversation: {', '.join(unsupported)}. The data already "
        "retrieved for this turn is already in the conversation above — do not call a tool "
        "again to look for it. Rewrite your reply using only values already retrieved or "
        "given by the user. For any value that was not retrieved, say plainly that it is "
        "not available, and file a data request if the task needs it. The rewrite must "
        "still answer the user's question in full, not just drop the unsupported figures."
    )
