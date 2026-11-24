"""Claim density: a deterministic count of checkable claims in a module's text.

A word count rewards length; a disclosure that says little in many words scores the same as one
that commits to numbers, dates and named places. `claim_density(text)` counts the specifics a
reader could check, as a counter-signal to `ReportModule.word_count` for length bias in the
engine's judging. It is a plain regex counter: no model, the same text always gives the same
numbers, and `DENSITY_VERSION` changes whenever a rule does so stored values can be recomputed
(`recompute_claim_density`, `tret disclosure claim-density`).

Categories, in the order they are matched. A matched span is masked before the next category
runs, so one token is counted once (`42% by 2030` is one `unit` and one `date`):

* `currency`: an amount with a symbol (`€ $ £ ¥`, also `US$`, `A$`...) or an ISO code (`EUR`,
  `USD`...) before it, or an ISO code / `euros` / `dollars` / `pounds` / `€` after it, with an
  optional scale word (`thousand million billion trillion bn mn m k`): `€12.5 million`,
  `USD 2.3bn`, `50 million euros`.
* `date`: full dates (`31 December 2024`, `December 31, 2024`, `2024-12-31`, `31/12/2024`), day and
  month, month and year, `Q3 2024`, `H1 2025`, `FY2024` / `FY24` / `FY2024/25`, and a stand-alone
  year 1900 to 2100 (not when a unit follows it: `2030 tCO2e` is a quantity, not a date).
* `unit`: a number, a number range, with a unit: `%`, percent, percentage points, basis points,
  `tCO2e` and its variants (`kt`, `Mt`, `Gt`, `CO2e`, `CO₂e`, `tCO2-e`, `tonnes CO2e`, a `/kWh`
  style denominator), tonnes, `kWh MWh GWh TWh`, `kW MW GW`, `GJ`, `m3 m³ m2`, litres, `ha`,
  hectares, `km`, `°C`, `ppm`. An optional scale word may sit between (`1.2 million tonnes`).
* `number`: any remaining number (integers and decimals, thousand separators in `1,234,567`,
  `1.234.567`, or a narrow no-break space; a range such as `5-10` is one number).
* `site`: a named place. A run of one to four capitalised words immediately before a site noun
  (`plant mine refinery site facility factory terminal mill smelter warehouse depot port farm
  field campus hub data centre/center`): `Rotterdam refinery`, `Port Hedland terminal`. Leading
  generic words (`The Our Each This ...`) are dropped from the run, so `Our refinery` is no site.
  Also `Gigafactory Berlin` and `Port of Rotterdam`.

Ignored before counting (structure, not claims): the `## Page N` / `## Section N` markers
`blocks.py` writes, heading numbering (`1.2.3 Title`, `## 4.1 Targets`), list markers (`1.`, `(a)`, `iv)`), footnote
markers (`[12]`) and superscript digits, URLs, and cross-references such as `Scope 3`,
`Table 4`, `Section 2`, `Article 8(1)`, `ISO 14001`, `GRI 305-1`, `ESRS E1`, `IFRS S2`, `p. 12`
(the number there names a place in a document, not a fact).

`words` is the same function as `ReportModule.word_count` (`extract.words`, run on the raw text),
so `per_100_words` is comparable with the stored word count.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from tret.evaluation import blocks as blk
from tret.evaluation.extract import words

DENSITY_VERSION = "claims-1"

CATEGORIES = ("currency", "date", "unit", "number", "site")

_MASK = "\x00"


@dataclass(frozen=True)
class ClaimDensity:
    words: int
    counts: dict[str, int]
    claims: int
    per_100_words: float
    version: str = DENSITY_VERSION


# ── noise ───────────────────────────────────────────────────────────────────────────────────
_LIST_MARKER = re.compile(
    r"^[ \t]*(?:[-*+•–][ \t]+)?(?:\d{1,2}[.)]|\((?:\d{1,2}|[A-Za-z]{1,4})\)|[A-Za-z][.)])[ \t]+", re.M
)
# `1.2.3 Title` (three levels or more, whatever follows) and `## 4.1 Targets` (two levels inside a
# Markdown heading). A bare two-level `2.5 Million tonnes ...` or `1.5 (2023: 1.2)` is a quantity.
_HEADING_NUMBER = re.compile(
    r"^[ \t]*(?:\d{1,2}(?:\.\d{1,2}){2,}\.?[ \t]+"
    r"|#{1,6}[ \t]+\d{1,2}(?:\.\d{1,2})+\.?[ \t]+)",
    re.M,
)
_FOOTNOTE = re.compile(r"\[(?:\d{1,3}|[a-z])\]")
# a superscript digit is a footnote marker, except after `m` where it is an exponent (m³, km²)
_SUPERSCRIPT = re.compile(r"(?<![mM])[⁰¹²³⁴⁵⁶⁷⁸⁹]+")
_URL = re.compile(r"https?://\S+|www\.\S+")
_REF_WORDS = (
    r"(?:(?i:tables?|figures?|fig\.|charts?|notes?|pages?|pp?\.|sections?|chapters?|articles?|annex(?:es)?|appendix|"
    r"scopes?|categor(?:y|ies))|"
    r"SDGs?|ISO|IAS|IFRS|GRI|ESRS|SASB|Principles?|Steps?|Phases?|Tiers?|Disclosures?|Requirements?|Goals?)"
)
# a reference id: `4`, `305-1`, `E1`, `S2`, `8(1)`, `14001:2015`; never a year or a thousands-grouped
# number (that is the claim after the reference: `Scope 2, 3,400 tCO2e`, `Section 4, 2024 targets`)
_NOT_CLAIM = r"(?!(?:19\d{2}|20\d{2}|2100)(?![\d.,]\d|\d))(?!\d{1,3}(?:[,\u202f\u00a0]\d{3}|\.\d{3}\.)\b)"
_REF_ID = _NOT_CLAIM + r"[A-Z]{0,2}\d+(?:[.\-–/]\d+)*(?::\d{4})?(?:\([A-Za-z0-9]{1,3}\))*"
# joiners: a comma, `and`, `&`, `to` (spaces allowed), or a TIGHT dash (`Scope 1-3`; a spaced
# dash is punctuation: `Scope 1 and 2 – 50% lower`); a joined id is not followed by %/units
_REF_JOIN = r"(?:[ \t]*(?:,|and|&|to)[ \t]*|[-–])"
_REFERENCE = re.compile(
    r"\b" + _REF_WORDS + r"[ \t]*" + _REF_ID + r"(?:" + _REF_JOIN + _REF_ID + r"(?![ \t]?(?:%|‰|°)))*(?![A-Za-z0-9])"
)


def _clean(text: str) -> str:
    out = []
    for line in text.splitlines():
        if blk.PAGE_RE.match(line) or blk.SECTION_RE.match(line):
            continue
        out.append(line)
    t = "\n".join(out)
    t = _URL.sub(" ", t)
    # references first, so a line-initial `p. 12` is not taken for a list marker
    t = _REFERENCE.sub(lambda m: _MASK * len(m.group(0)), t)
    t = _HEADING_NUMBER.sub("", t)
    t = _LIST_MARKER.sub("", t)
    t = _FOOTNOTE.sub("", t)
    return _SUPERSCRIPT.sub("", t)


# ── patterns ────────────────────────────────────────────────────────────────────────────────
_NUM = (
    r"(?:\d{1,3}(?:[,  ]\d{3})+(?:\.\d+)?|\d{1,3}(?:\.\d{3}){2,}(?:,\d+)?|\d+(?:[.,]\d+)?)"
)
_NUM_RANGE = r"(?:" + _NUM + r"[ \t]?[-–][ \t]?)?" + _NUM
_SCALE = r"(?i:thousand|million|billion|trillion|bn|mn|m|k|tn)(?![A-Za-z0-9])"
_SCALE_OPT = r"(?:[ \t]?" + _SCALE + r")?"
_BEFORE = r"(?<![\w.,])"

_ISO = (
    r"(?:EUR|USD|GBP|CHF|SEK|NOK|DKK|JPY|CNY|RMB|AUD|CAD|NZD|HKD|SGD|INR|BRL|ZAR|MXN|PLN|CZK|HUF|KRW|AED|SAR|"
    r"TRY|RUB|THB|IDR|MYR|PHP|ILS|TWD|CLP|COP|ARS|EGP|NGN|KES)"
)
_CUR_SYMBOL = r"(?<![A-Za-z0-9])(?:US|AU|CA|NZ|HK|[ACSR])?[€$£¥]"
_CURRENCY = re.compile(
    "(?:"
    + _CUR_SYMBOL + r"[ \t]?" + _NUM + _SCALE_OPT
    + r"|\b" + _ISO + r"[ \t]?" + _NUM + _SCALE_OPT
    + r"|" + _BEFORE + _NUM + _SCALE_OPT + r"[ \t]?(?:" + _ISO + r"\b|(?i:euros?|dollars?|pounds?|pence|cents?)\b|[€£¥])"
    + ")"
)

_MONTH = (
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\.?"
)
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
_YEAR4 = r"(?:19|20)\d{2}"
_UNIT_WORDS = (
    r"(?:[kMGT]?t[ \t]?CO(?:2|₂)|[kMGT]?Wh|[kMGT]Wp?|[kMGTP]J|m3|m²|m³|m2|km2|km²|ha|km|kg|ppm|ppb|ML|kL|"
    r"[kMG]?t|Mboe|boe|bbl|MMBtu|BTU|Btu|"
    r"(?i:tonnes?|tons?|metric[ \t]tons?|kilotonnes?|megatonnes?|gigatonnes?|hectares?|litres?|liters?|"
    r"megalitres?|megaliters?|kilomet(?:re|er)s?|miles?|percent|per[ \t]cent|percentage[ \t]points?|"
    r"basis[ \t]points?|bps|pp|degrees?[ \t]C(?:elsius)?))"
)
_CO2E = (
    r"(?:(?:[kMGT]?t|kg|g|(?i:tonnes?|tons?|metric[ \t]tons?))(?:[ \t]of)?[ \t]?)?CO(?:2|₂)[ \t]?[-‑]?"
    r"(?:e(?:q|quivalent)?)?(?![A-Za-z0-9])"
)
_UNIT = (
    r"(?:%|‰|°[ \t]?[CF]|º[ \t]?[CF]|(?:" + _CO2E + r"|" + _UNIT_WORDS + r")(?![A-Za-z0-9])(?:/[A-Za-z][A-Za-z0-9²³]*)?)"
)
_UNIT_RE = re.compile(_BEFORE + _NUM_RANGE + r"(?:[ \t]?(?i:thousand|million|billion|trillion|bn|mn|k)(?![A-Za-z0-9]))?[ \t  ]?" + _UNIT)

_DATE = re.compile(
    "(?:"
    r"\b" + _YEAR4 + r"-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b"
    r"|\b(?:0?[1-9]|[12]\d|3[01])[/.](?:0?[1-9]|1[0-2])[/.](?:\d{4}|\d{2})\b"
    r"|\b" + _DAY + r"[ \t]+(?:of[ \t]+)?" + _MONTH + r"(?:,?[ \t]+" + _YEAR4 + r")?(?![A-Za-z0-9])"
    r"|\b" + _MONTH + r"[ \t]+" + _DAY + r"(?:,?[ \t]+" + _YEAR4 + r")?(?![A-Za-z0-9])"
    r"|\b" + _MONTH + r"[ \t]+" + _YEAR4 + r"\b"
    r"|\bQ[1-4][ \t]?(?:FY[ \t]?)?(?:'?\d{2}\b|" + _YEAR4 + r"\b)"
    r"|\bH[12][ \t]?(?:FY[ \t]?)?(?:'?\d{2}\b|" + _YEAR4 + r"\b)"
    r"|\bFY[ \t-]?(?:" + _YEAR4 + r"(?:[/-]\d{2,4})?|\d{2})\b"
    r"|(?<![\w.,])(?:19\d{2}|20\d{2}|2100)(?![\w]|[.,]\d)(?![ \t]?" + _UNIT + r")"
    ")"
)

_NUMBER = re.compile(_BEFORE + _NUM_RANGE + r"(?!\w)")

_SITE_NOUN = (
    r"(?:plants?|mines?|refiner(?:y|ies)|sites?|facilit(?:y|ies)|factor(?:y|ies)|terminals?|mills?|smelters?|"
    r"warehouses?|depots?|ports?|farms?|fields?|campus(?:es)?|hubs?|data[ \t]cent(?:re|er)s?)"
)
_CAP = r"[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ'’\-]*"
# a name starts only at the beginning of a token (not after `-`, `'` inside one): keeps matching linear
_TOKEN_START = r"(?<![\w'’\-])"
_SITE_NAMED = re.compile(_TOKEN_START + r"((?:" + _CAP + r")(?:[ \t]+" + _CAP + r"){0,3})[ \t]+" + _SITE_NOUN + r"(?![A-Za-z0-9])")
_SITE_PREFIXED = re.compile(
    _TOKEN_START + r"(?:Gigafactory|Port(?:[ \t]+of)?)[ \t]+" + _CAP + r"(?:[ \t]+" + _CAP + r"){0,2}"
)
_GENERIC = frozenset(
    "The Our Each This That These Those Its Their His Her A An All Any Every Some Such Many Most Other Another "
    "New Existing Former Several Both One Two Three Four Five Six Seven Eight Nine Ten No Each Per Main Local "
    "Key Major Further Additional Group Company We".split()
)


def _mask(text: str, pattern: re.Pattern[str]) -> tuple[str, int]:
    n = 0

    def sub(m: re.Match[str]) -> str:
        nonlocal n
        n += 1
        return _MASK * len(m.group(0))

    return pattern.sub(sub, text), n


def _count_sites(text: str) -> tuple[str, int]:
    text, n = _mask(text, _SITE_PREFIXED)

    def sub(m: re.Match[str]) -> str:
        nonlocal n
        spans = list(re.finditer(r"\S+", m.group(1)))
        k = 0
        while k < len(spans) and spans[k].group(0) in _GENERIC:
            k += 1
        if k == len(spans):  # nothing but generic words: not a named site
            return m.group(0)
        n += 1
        keep = (m.start(1) - m.start(0)) + spans[k].start()  # keep the dropped leading words
        whole = m.group(0)
        return whole[:keep] + _MASK * (len(whole) - keep)

    return _SITE_NAMED.sub(sub, text), n


def claim_density(text: str) -> ClaimDensity:
    """Count the checkable claims in `text` (see the module docstring for the rules)."""
    w = words(text or "")
    counts = dict.fromkeys(CATEGORIES, 0)
    t = _clean(text or "")
    t, counts["currency"] = _mask(t, _CURRENCY)
    t, counts["date"] = _mask(t, _DATE)
    t, counts["unit"] = _mask(t, _UNIT_RE)
    t, counts["number"] = _mask(t, _NUMBER)
    t, counts["site"] = _count_sites(t)
    claims = sum(counts.values())
    per = round(claims * 100.0 / w, 2) if w else 0.0
    return ClaimDensity(words=w, counts=counts, claims=claims, per_100_words=per)


# ── backfill ────────────────────────────────────────────────────────────────────────────────
async def recompute_claim_density(
    db: Any, *, workspace_id: uuid.UUID, batch_id: uuid.UUID | None = None, force: bool = False
) -> dict[str, int]:
    """Recompute and store the density of every present module with text whose `density_version`
    differs from `DENSITY_VERSION` (every one with `force`). `batch_id` limits it to the
    extractions of that batch (the source collection batch or the extraction batch). The text is
    read from the blob store; a module whose blob is missing is skipped and counted, never an
    error. Flushes, never commits. Returns `{considered, updated, skipped_current, missing_text}`."""
    from sqlalchemy import select

    from tret.db.models import ReportExtraction, ReportModule
    from tret.evaluation.review import extraction_ids_for_batch
    from tret.storage import get_store

    stmt = (
        select(ReportModule)
        .join(ReportExtraction, ReportExtraction.id == ReportModule.extraction_id)
        .where(ReportExtraction.workspace_id == workspace_id, ReportModule.text_key.is_not(None))
    )
    if batch_id is not None:
        stmt = stmt.where(ReportExtraction.id.in_(await extraction_ids_for_batch(db, workspace_id, batch_id)))
    stats = {"considered": 0, "updated": 0, "skipped_current": 0, "missing_text": 0}
    store = get_store()
    for m in (await db.execute(stmt)).scalars().all():
        stats["considered"] += 1
        if not force and m.density_version == DENSITY_VERSION:
            stats["skipped_current"] += 1
            continue
        try:
            raw = await store.get(m.text_key)
        except FileNotFoundError:
            stats["missing_text"] += 1
            continue
        store_density(m, claim_density(raw.decode("utf-8", errors="replace")))
        stats["updated"] += 1
    await db.flush()
    return stats


def store_density(module: Any, d: ClaimDensity) -> None:
    """Write a `ClaimDensity` onto a `ReportModule` row."""
    module.claim_count = d.claims
    module.claim_density = d.per_100_words
    module.claim_counts = dict(d.counts)
    module.density_version = d.version
