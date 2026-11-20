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

import bisect
import itertools
import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import NamedTuple

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
#           LETTER SUFFIX (up to three letters: `C`, `bn`, `kWh`) *not* captured
#           into the value, so `1.5C` extracts `1.5` and `$2.5bn` extracts `2.5`
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
    r"(?:(?P<dec>\d[\d,]*\.\d+%?)(?:[A-Za-z]{1,3})?(?![\w])|(?P<int>\d[\d,]*%?)(?![\w])"
    # `glued` - a bare integer fused to a recognised unit ("4x", "5MW", "3yr"):
    # the unit is not captured, and the figure counts as unit-bearing (R12).
    r"|(?P<glued>\d[\d,]*)(?=(?:x|yrs?|kWh|MWh|GWh|kW|MW|GW|kg|tCO2e|bn|m|k|t)(?![A-Za-z0-9_])))"
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


class _Tok(NamedTuple):
    raw: str
    norm: str
    start: int
    end: int


# What makes a small bare integer a claimed figure after all (R12): a unit
# word or symbol right behind it ("3 yr", "2 months", "4x", "5 MW") or a
# currency symbol right before it ("$5"). "3 datasets" stays exempt.
_UNIT_WORDS = (
    r"yrs?\b|years?\b|months?\b|mos?\b|[kMGT]?Wh?\b|tCO2e?\b|kg\b|lbs?\b"
    r"|tonnes?\b|tons?\b|mt\b|gal(?:lons?)?\b|therms?\b|usd\b|eur\b|bn\b"
)
_UNIT_AFTER_RE = re.compile(
    # spaced or glued: "5 yr", "5%", "5x"; hyphenated words: "5-year", "3-month"
    rf"[ \t]?(?:%|×|x\b|t\b|{_UNIT_WORDS})|-(?:{_UNIT_WORDS})",
    re.IGNORECASE,
)
_CURRENCY_CHARS = "$€£¥"


def _has_unit_or_currency(text: str, start: int, end: int) -> bool:
    if _UNIT_AFTER_RE.match(text, end):
        return True
    i = start - 1
    if i >= 0 and text[i] in " \t":
        i -= 1
    return i >= 0 and text[i] in _CURRENCY_CHARS


def _scan_tokens(text: str, *, lenient: bool = False) -> list[_Tok]:
    """Every numeric token in `text` with its position, in order.

    Exemptions (skipped when `lenient`, which evidence extraction uses — what
    the run saw includes every number, small ones and ordinals too):

    * a markdown list ordinal at the start of its line (`1. `, `2) `);
    * any piece of an ISO date (`_ISO_DATE_RE`) — applies to evidence too;
    * a bare integer under `_EXEMPT_BELOW` with no decimal point, no `%`, no
      unit word right behind it and no currency symbol right before it.
    """
    if not text:
        return []
    date_spans = [m.span() for m in _ISO_DATE_RE.finditer(text)]
    out: list[_Tok] = []
    for m in _NUMBER_RE.finditer(text):
        if any(m.start() < de and m.end() > ds for ds, de in date_spans):
            continue  # inside an ISO date — not a claimed figure at all
        # The `dec` branch may have consumed a trailing unit letter (`1.5C`)
        # that is not part of the value, so the raw surface form comes from
        # whichever named branch matched, never from the whole match.
        glued = m.group("glued")
        raw = m.group("dec") or m.group("int") or glued
        # A comma after the number is punctuation, not a thousands separator.
        stripped = raw.rstrip(",")
        end = m.start() + len(stripped) if glued else m.end()
        raw = stripped
        if not lenient:
            line_start = text.rfind("\n", 0, m.start()) + 1
            if text[line_start : m.start()].strip() == "":
                after = text[m.end() : m.end() + 2]
                if after[:1] in (".", ")") and (len(after) < 2 or after[1].isspace()):
                    continue  # markdown list ordinal, e.g. "1. " / "2) "
        try:
            normalized = _normalize(raw)
        except InvalidOperation:  # pragma: no cover - regex shouldn't allow this
            continue
        if not lenient and "." not in raw and "%" not in raw and not glued:
            try:
                small = abs(int(normalized)) < _EXEMPT_BELOW
            except ValueError:  # pragma: no cover
                small = False
            if small and not _has_unit_or_currency(text, m.start(), m.end()):
                continue
        out.append(_Tok(raw, normalized, m.start(), end))
    return out


def _iter_number_tokens(text: str, *, lenient: bool = False) -> list[tuple[str, str]]:
    """`(raw surface form, normalized value)` for every non-exempt numeric
    token in `text`, in the order they appear."""
    return [(t.raw, t.norm) for t in _scan_tokens(text, lenient=lenient)]


def extract_numbers(text: str, *, lenient: bool = False) -> set[str]:
    """The set of normalized numeric values present in `text`."""
    return {t.norm for t in _scan_tokens(text, lenient=lenient)}


# ── what part of a reply is checked (R11) ────────────────────────────────────
# Code is not prose: a stylesheet's `padding: 5%` is not a claimed figure.
_SKIP_LANGS = frozenset(
    {
        "css", "scss", "sass", "less", "js", "javascript", "jsx", "ts", "typescript", "tsx",
        "python", "py", "sh", "bash", "shell", "zsh", "sql",
    }
)
_HTML_LANGS = frozenset({"html", "htm", "svg", "xml"})
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*([\w+#.-]*)")
_INLINE_CODE_RE = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_HTML_DOC_RE = re.compile(r"\s*(?:<!doctype\s+html|<html\b)", re.IGNORECASE)
_HTML_DROP_RE = re.compile(
    r"<!--.*?(?:-->|\Z)"
    r"|<(style|script)\b[^>]*>.*?(?:</\1\s*>|\Z)"
    r"|<(template|noscript)\b[^>]*>.*?(?:</\2\s*>|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG_RE = re.compile(
    r"</?[A-Za-z!](?:\"[^\"]*\"|'[^']*'|[^<>\"'])*(?:>|\Z)|&#?\w+;"
)
_LIST_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")


def _blank(s: str) -> str:
    """Same length, same newlines, nothing else — so offsets and line numbers
    in the masked text are those of the original."""
    return re.sub(r"[^\n]", " ", s)


def _mask_html(s: str) -> tuple[str, int]:
    """Visible text of an HTML fragment, blanked in place; plus how many
    style/script blocks were dropped."""
    dropped = 0

    def drop(m: re.Match) -> str:
        nonlocal dropped
        if (m.group(1) or "").lower() in ("style", "script"):
            dropped += 1
        return _blank(m.group(0))

    s = _HTML_DROP_RE.sub(drop, s)
    return _HTML_TAG_RE.sub(lambda m: _blank(m.group(0)), s), dropped


def _fence_spans(text: str) -> list[tuple[int, int, int, str]]:
    """`(span_start, body_start, span_end, lang)` for each fenced block. An
    unclosed fence (a truncated reply) runs to the end of the text."""
    spans: list[tuple[int, int, int, str]] = []
    pos = 0
    n = len(text)
    while pos < n:
        eol = text.find("\n", pos)
        line_end = n if eol == -1 else eol
        m = _FENCE_OPEN_RE.match(text[pos:line_end])
        if not m:
            pos = line_end + 1
            continue
        marker, lang = m.group(1), m.group(2).lower()
        body_start = min(n, line_end + 1)
        scan = body_start
        end = n
        while scan < n:
            e2 = text.find("\n", scan)
            le = n if e2 == -1 else e2
            stripped = text[scan:le].strip()
            if stripped and set(stripped) == {marker[0]} and len(stripped) >= len(marker):
                end = le
                break
            scan = le + 1
        spans.append((pos, body_start, end, lang))
        pos = min(n, end + 1)
    return spans


_CODE_CHARS_RE = re.compile(r"[{}()\[\]=<>;\\/_@#&|^*+~]|\s-\s|->|:\S|\w:\w|\d\.\d+\.\d")
_CSS_UNIT_RE = re.compile(r"[\d.,]+\s*(?:px|r?em|pt|vh|vw|vmin|vmax|ch|%)", re.IGNORECASE)
# A span that is only a figure: optional currency, the number, optional unit words.
_PLAIN_FIGURE_RE = re.compile(
    r"[$€£¥]?\s*\d[\d,]*(?:\.\d+)?\s*%?\s*(?:[A-Za-z][A-Za-z0-9]{0,11}(?:\s+[A-Za-z][A-Za-z0-9]{0,11})?)?"
)
_IDENT_RE = re.compile(r"[A-Za-z]+[-_.]?\d|\d[-_.]?[A-Za-z]|[a-z][A-Z]|_")


def _looks_like_code(span: str) -> bool:
    """Is an inline `span` code (skipped by the number check) rather than a
    figure the reply wrapped in backticks? Code: an identifier mixing letters and
    digits/underscores (`CW-010`, `run_id`), an operator, a path/URL, braces,
    `=`, or a CSS length (`12px`). A span that is just a number with an optional
    unit or currency (`98765 tCO2e`, `$5`) is a claim and is checked."""
    s = span.strip()
    if _CSS_UNIT_RE.fullmatch(s) or _CODE_CHARS_RE.search(s):
        return True
    if _PLAIN_FIGURE_RE.fullmatch(s):
        return False
    return bool(_IDENT_RE.search(s))


def _mask_reply(text: str) -> tuple[str, list[tuple[int, int]], int]:
    """`(masked text, html spans, skipped code blocks)`.

    Fenced css/js/python/sh/sql blocks and inline code spans are blanked;
    html/svg/xml fences, and a reply that is itself an HTML document, keep only
    their visible text (tags, attributes, style and script are blanked). Other
    fences (json, csv, text, none) are checked as prose.
    """
    if _HTML_DOC_RE.match(text):
        masked, dropped = _mask_html(text)
        return masked, [(0, len(text))], dropped
    out: list[str] = []
    html_spans: list[tuple[int, int]] = []
    skipped = 0
    cursor = 0

    def plain(seg: str) -> str:
        return _INLINE_CODE_RE.sub(
            lambda m: _blank(m.group(0)) if _looks_like_code(m.group(2)) else m.group(0), seg
        )

    for span_start, body_start, span_end, lang in _fence_spans(text):
        out.append(plain(text[cursor:span_start]))
        if lang in _SKIP_LANGS:
            out.append(_blank(text[span_start:span_end]))
            skipped += 1
        elif lang in _HTML_LANGS:
            masked_body, dropped = _mask_html(text[body_start:span_end])
            out.append(text[span_start:body_start] + masked_body)
            html_spans.append((body_start, span_end))
            skipped += dropped
        else:
            out.append(text[span_start:span_end])
        cursor = span_end
    out.append(plain(text[cursor:]))
    return "".join(out), html_spans, skipped


def _line_info(masked: str, html_spans: list[tuple[int, int]]):
    """Per-line starts, block ids and kinds, for `where` and for keeping
    arithmetic inside one block: a whole table (any rows), a whole list, one
    paragraph, or one html line. Unrelated paragraphs never share an id."""
    starts: list[int] = []
    para: list[int] = []
    kinds: list[str] = []
    pid = 0
    pos = 0
    prev_blank = True
    prev_kind = ""  # kind of the previous line, "" after a blank one
    last_kind = ""  # kind of the last non-blank line
    for line in masked.split("\n"):
        starts.append(pos)
        stripped = line.strip()
        in_html = any(s <= pos < e or s < pos + len(line) <= e for s, e in html_spans)
        if not stripped:
            prev_blank = True
            kind = "prose"
            prev_kind = ""
        else:
            if in_html:
                kind = "html"
                pid += 1
            elif stripped.startswith("|"):
                kind = "table"
                if prev_kind != "table":
                    pid += 1
            elif _LIST_LINE_RE.match(line):
                kind = "list"
                # Items of one list (blank lines between them included) are one block.
                if last_kind != "list":
                    pid += 1
            else:
                kind = "prose"
                if prev_blank:
                    pid += 1
            prev_blank = kind in ("html", "table")
            prev_kind = kind
            last_kind = kind
        para.append(pid)
        kinds.append(kind)
        pos += len(line) + 1
    return starts, para, kinds


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


# A table row whose first cell says it totals other rows: the only table figure
# that cross-row arithmetic may support (R2).
_TOTALS_ROW_RE = re.compile(r"total|subtotal|sum|overall|all companies|portfolio", re.I)

# First line of a read of a document Tret itself wrote: never evidence.
GENERATED_MARKER = "[tret:generated — written by Tret in this workspace; not evidence]"

_DRAFT_DOC_ID_RE = re.compile(r"document ([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})")


def _calls_by_id(messages: list[Msg]) -> dict[str, tuple[str, dict]]:
    """`tool_call_id -> (tool name, arguments)` for every assistant tool call."""
    out: dict[str, tuple[str, dict]] = {}
    for msg in messages:
        if msg.role != "assistant":
            continue
        for call in msg.tool_calls:
            out[call.id] = (call.name, call.arguments if isinstance(call.arguments, dict) else {})
    return out


def evidence_numbers(
    *,
    system: str,
    messages: list[Msg],
    task_input: dict,
    retrieved: list[dict] | None = None,
    exclude_document_ids: set[str] | None = None,
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

    Documents the model wrote itself are not evidence either (M4): a
    `draft_file` result, and a `read_document` of a document in
    `exclude_document_ids` (the run's `source_kind="generated"` documents; the
    ids `draft_file` reported in this run are added automatically), would let an
    invented figure launder itself through draft -> read.
    """
    numbers: set[str] = set(extract_numbers(system or "", lenient=True))
    call_args = _call_arguments_by_id(messages)
    calls = _calls_by_id(messages)
    generated_ids = {str(d).lower() for d in (exclude_document_ids or ())}
    for msg in messages:
        if msg.role == "tool" and calls.get(msg.tool_call_id, ("", {}))[0] == "draft_file":
            generated_ids.update(i.lower() for i in _DRAFT_DOC_ID_RE.findall(msg.content or ""))

    for msg in messages:
        meta = msg.meta or {}
        if msg.role == "tool":
            name, args = calls.get(msg.tool_call_id, ("", {}))
            if name == "draft_file" or (msg.content or "").lstrip().startswith(GENERATED_MARKER):
                continue
            if name == "read_document" and str(args.get("document_id", "")).lower() in generated_ids:
                continue
            result_numbers = extract_numbers(msg.content or "", lenient=True)
            call_json = call_args.get(msg.tool_call_id)
            if call_json:
                result_numbers -= extract_numbers(call_json, lenient=True)
            numbers |= result_numbers
        elif msg.role == "user":
            if meta.get(ENGINE_NUDGE_KEY) is not None or meta.get("budget_line"):
                continue
            numbers |= extract_numbers(msg.content or "", lenient=True)
        # assistant-role messages are never evidence — see docstring.

    numbers |= extract_numbers(task_input.get("message") or "", lenient=True)
    for entry in task_input.get("_history") or []:
        if isinstance(entry, dict) and entry.get("role") == "user":
            numbers |= extract_numbers(entry.get("content") or "", lenient=True)
    for entry in retrieved or []:
        numbers |= extract_numbers(str(entry.get("value", "")), lenient=True)

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


def _context(reply: str, start: int, end: int) -> str:
    snippet = " ".join(reply[max(0, start - 50) : end + 50].split())
    return snippet if len(snippet) <= 120 else snippet[:119] + "…"


def scan_reply(reply: str, evidence: set[str]) -> dict:
    """The full number check on one reply: `{"details": [...],
    "skipped_code_blocks": int}`; `details` is what `unsupported_details`
    returns. See `unsupported_numbers` for the rules."""
    masked, html_spans, skipped = _mask_reply(reply or "")
    tokens = _scan_tokens(masked)
    if not tokens:
        return {"details": [], "skipped_code_blocks": skipped}

    starts, para, kinds = _line_info(masked, html_spans)
    evidence_decimals = [d for d in (_as_decimal(v) for v in evidence) if d is not None]

    def line_of(pos: int) -> int:
        return bisect.bisect_right(starts, pos) - 1

    masked_lines = masked.split("\n")

    def is_totals_row(li: int) -> bool:
        cells = masked_lines[li].strip().strip("|").split("|")
        return bool(_TOTALS_ROW_RE.search(cells[0])) if cells else False

    info = []  # (token, line index, block key, supported by (a)-(c))
    supported_cache: dict[tuple[str, str], bool] = {}
    pools: dict[object, dict[str, Decimal]] = {}
    for t in tokens:
        li = line_of(t.start)
        if kinds[li] == "html":
            block = ("line", li)
        elif kinds[li] == "table":
            # R2: operands of a table figure share its row, unless it is a totals row.
            block = ("row", li)
        else:
            block = ("para", para[li])
        key = (t.raw, t.norm)
        if key not in supported_cache:
            supported_cache[key] = _supported_by_evidence(t.raw, t.norm, evidence, evidence_decimals)
        ok = supported_cache[key]
        info.append((t, li, block, ok))
        if ok:
            val = _as_decimal(t.norm)
            if val is not None:
                pools.setdefault(block, {})[t.norm] = val
                if block[0] == "row":
                    pools.setdefault(("para", para[li]), {})[t.norm] = val

    seen: set[str] = set()
    details: list[dict] = []
    for t, li, block, ok in info:
        if ok or t.norm in seen:
            continue
        val = _as_decimal(t.norm)
        if val is None:  # pragma: no cover - normalized is always Decimal-parseable
            continue
        # Rule (d), only over supported figures in the SAME paragraph, list,
        # table (any row) or html line as the claim (R12) - a long reply must
        # not find "support" by pairing numbers from unrelated sections.
        tolerance = _ARITHMETIC_TOLERANCE_UNITS.scaleb(-_decimal_places(t.raw))
        if block[0] == "row" and is_totals_row(li):
            block = ("para", para[li])
        pool = pools.get(block, {})
        if any(
            abs(val - (yv + zv)) <= tolerance
            or abs(val - abs(yv - zv)) <= tolerance
            or abs(val - (yv + zv) / 2) <= tolerance
            for yv, zv in itertools.combinations_with_replacement(pool.values(), 2)
        ):
            continue
        seen.add(t.norm)
        details.append(
            {
                "value": t.raw,
                "where": kinds[li],
                "line": li + 1,
                "context": _context(reply, t.start, t.end),
            }
        )
    return {"details": details, "skipped_code_blocks": skipped}


def unsupported_details(reply: str, evidence: set[str]) -> list[dict]:
    """`unsupported_numbers`, with where each figure sits: `[{value, where
    ("prose"|"table"|"list"|"html"), line (1-based), context (<=120 chars)}]`,
    same rules, same order, first occurrence of each value."""
    return scan_reply(reply, evidence)["details"]


def skipped_code_blocks(reply: str) -> int:
    """How many code blocks (css/js/python/sh/sql fences, html style/script)
    the number check skipped in `reply`."""
    return _mask_reply(reply or "")[2]


def unsupported_numbers(reply: str, evidence: set[str]) -> list[str]:
    """Numbers `reply` states that `evidence` does not support, by any of:

    (a) an exact normalized match (`58` against evidence `58`);
    (b) rounding - see `_rounds_to_some_evidence`;
    (c) percent/fraction - see `_percent_or_fraction_match`;
    (d) arithmetic - the token equals the sum, difference, or mean of two
        OTHER tokens in the same paragraph, list, table (any row) or html line
        that are themselves supported by (a)-(c), within
        `_ARITHMETIC_TOLERANCE_UNITS` of the *claimed* token's own last decimal
        place. Deliberately does not chain, and does not reach across
        unrelated paragraphs.

    What is read: prose, tables, lists, json/csv/text fences, and the visible
    text of html (tags, attributes, style and script are dropped); code
    fences (css, js, python, sh, sql) and inline code are skipped. A bare
    integer under 10 is exempt unless a unit or currency symbol sits next to
    it ("3 yr", "$5" are checked; "3 datasets" is not).

    Returned as they appeared in `reply`, deduplicated, in order of first
    appearance.
    """
    return [d["value"] for d in unsupported_details(reply, evidence)]


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
