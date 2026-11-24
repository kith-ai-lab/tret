"""Loaders and pure functions for the peer taxonomy (see `tret/taxonomy/data/README.md`)."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any, Iterable

# Bump whenever a data file changes classification results.
TAXONOMY_VERSION = "nace2-tret-1"

# Tret industry clusters (ours): key -> label. Every division maps to exactly one.
CLUSTERS: dict[str, str] = {
    "agriculture_forestry": "Agriculture, forestry and fishing",
    "mining_metals": "Mining and metals",
    "oil_gas": "Oil and gas",
    "utilities": "Utilities",
    "waste_water": "Waste and sewerage",
    "chemicals": "Chemicals, rubber and plastics",
    "paper_packaging": "Paper and packaging",
    "construction_materials": "Construction materials and wood products",
    "construction_engineering": "Construction and engineering",
    "capital_goods": "Capital goods, machinery and aerospace",
    "autos": "Automotive and vehicles",
    "technology_hardware": "Technology hardware and electronics",
    "software_it_services": "Software and IT services",
    "telecom_media": "Telecom, media and advertising",
    "pharma_biotech": "Pharmaceuticals and biotechnology",
    "medical_devices": "Medical devices",
    "healthcare_services": "Healthcare services",
    "food_beverage_tobacco": "Food, beverage and tobacco",
    "consumer_goods": "Consumer goods",
    "retail": "Retail",
    "wholesale_distribution": "Wholesale and distribution",
    "hospitality_leisure": "Hospitality, travel and leisure",
    "transport": "Transport and logistics",
    "real_estate": "Real estate",
    "banks": "Banks",
    "insurance": "Insurance",
    "diversified_financials": "Diversified financials and asset management",
    "business_services": "Business and professional services",
    "public_social_other": "Public, social and other",
}

LEVELS = ("group", "division", "cluster", "section")
MATCHES = ("exact", "close", "broad", "none")  # none: the SIC code has no industry
SOURCE_PRECEDENCE = ("manual", "sec_sic", "extractor")  # most trusted first


def _read_csv(name: str) -> list[dict[str, str]]:
    text = (resources.files("tret.taxonomy") / "data" / name).read_text(encoding="utf-8")
    return list(csv.DictReader(io.StringIO(text)))


@lru_cache(maxsize=1)
def _nace() -> dict[str, dict[str, dict[str, str]]]:
    out: dict[str, dict[str, dict[str, str]]] = {"section": {}, "division": {}, "group": {}}
    for r in _read_csv("nace_rev2.csv"):
        out[r["level"]][r["code"]] = {"code": r["code"], "parent": r["parent"], "title": r["title"]}
    return out


@lru_cache(maxsize=1)
def _cluster_map() -> dict[str, str]:
    return {r["code"]: r["cluster"] for r in _read_csv("nace_clusters.csv")}


def sections() -> dict[str, dict[str, str]]:
    return dict(_nace()["section"])


def divisions() -> dict[str, dict[str, str]]:
    return dict(_nace()["division"])


def groups() -> dict[str, dict[str, str]]:
    return dict(_nace()["group"])


def cluster_of(code: str) -> str:
    """The cluster of a validated division ("21") or group ("21.2") code: a group-level
    override when the group has one, else its division's cluster."""
    cm = _cluster_map()
    if code in cm:
        return cm[code]
    return cm[code.split(".")[0]]


_CODE = re.compile(r"^\s*(\d{2})(?:\.?(\d{1,2}))?\s*$")


def ladder(nace_code: str | int) -> dict[str, Any]:
    """The pool ladder of a NACE Rev. 2 code: `{group, division, cluster, section}`.

    Accepts a division ("26"), a group ("26.1" or "261") or a class ("26.11" or "2611", cut
    back to its group). `group` is None for a division-level code. Raises ValueError for a
    malformed code or one that is not in NACE Rev. 2.
    """
    raw = str(nace_code).strip() if nace_code is not None else ""
    m = _CODE.match(raw)
    if not m:
        raise ValueError(f"not a NACE Rev. 2 code: {nace_code!r}")
    div, rest = m.group(1), m.group(2)
    nace = _nace()
    if div not in nace["division"]:
        raise ValueError(f"unknown NACE Rev. 2 division: {div!r}")
    group = None
    if rest:
        group = f"{div}.{rest[0]}"
        if group not in nace["group"]:
            raise ValueError(f"unknown NACE Rev. 2 group: {group!r}")
    return {
        "group": group,
        "division": div,
        "cluster": cluster_of(group or div),
        "section": nace["division"][div]["parent"],
    }


def pool_key(level: str, lad: dict[str, Any]) -> str:
    """The peer-pool key of a ladder at one level, e.g. `division:21`, `cluster:banks`."""
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}, got {level!r}")
    value = lad.get(level)
    if not value:
        raise ValueError(f"this ladder has no {level} (a division-level code): {lad!r}")
    return f"{level}:{value}"


# ── SIC ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SicMapping:
    sic: str
    sic_title: str
    nace_division: str | None  # None when match is "none"
    nace_group: str | None
    match: str
    note: str

    @property
    def classifiable(self) -> bool:
        """False for a code with no industry (blank checks, depositary receipts, ...)."""
        return self.match != "none" and bool(self.nace_division)

    def ladder(self) -> dict[str, Any]:
        if not self.classifiable:
            raise ValueError(f"SIC {self.sic} ({self.sic_title}) has no industry to classify")
        return ladder(self.nace_group or self.nace_division)


@lru_cache(maxsize=1)
def _sic() -> dict[str, SicMapping]:
    return {
        r["sic"]: SicMapping(
            sic=r["sic"], sic_title=r["sic_title"], nace_division=r["nace_division"] or None,
            nace_group=r["nace_group"] or None, match=r["match"], note=r["note"],
        )
        for r in _read_csv("sic_nace.csv")
    }


def sic_codes() -> list[str]:
    return sorted(_sic())


def classify_sic(sic: str | int | None) -> SicMapping | None:
    """The NACE mapping of a 4-digit SIC code (int or string, "2834", "02834" or 2834),
    or None when it is empty, malformed or not in the concordance. A code with no industry
    (6770 blank checks, ...) returns a mapping with `match == "none"` and `classifiable` False;
    its `ladder()` raises ValueError."""
    if sic is None or isinstance(sic, bool):
        return None
    s = str(sic).strip()
    if not s.isdigit() or len(s) > 4:
        return None
    return _sic().get(s.zfill(4))


# ── GICS cross-check ─────────────────────────────────────────────────────────
# Coarse: the clusters each GICS sector may legitimately contain. Deliberately generous (a
# GICS sector is cut differently from NACE); a disagreement is a flag for a human, never
# something to auto-resolve.
_GICS: dict[str, frozenset[str]] = {
    "energy": frozenset({"oil_gas", "mining_metals", "capital_goods", "transport", "utilities"}),
    "materials": frozenset({
        "chemicals", "construction_materials", "mining_metals", "paper_packaging",
        "agriculture_forestry", "capital_goods",
    }),
    "industrials": frozenset({
        "capital_goods", "transport", "construction_engineering", "business_services",
        "wholesale_distribution", "waste_water", "construction_materials",
        "technology_hardware", "public_social_other",
    }),
    "consumer discretionary": frozenset({
        "autos", "consumer_goods", "retail", "hospitality_leisure", "wholesale_distribution",
        "business_services", "real_estate", "construction_engineering",
        "technology_hardware", "capital_goods", "transport",
    }),
    "consumer staples": frozenset({
        "food_beverage_tobacco", "consumer_goods", "retail", "wholesale_distribution",
        "agriculture_forestry", "chemicals", "paper_packaging",
    }),
    "health care": frozenset({
        "pharma_biotech", "medical_devices", "healthcare_services", "wholesale_distribution",
        "capital_goods", "business_services", "insurance", "retail",
    }),
    "financials": frozenset({
        "banks", "insurance", "diversified_financials", "business_services",
        "software_it_services", "real_estate",
    }),
    "information technology": frozenset({
        "software_it_services", "technology_hardware", "business_services", "capital_goods",
        "wholesale_distribution",
    }),
    "communication services": frozenset({
        "telecom_media", "software_it_services", "hospitality_leisure", "retail",
    }),
    "utilities": frozenset({"utilities", "waste_water", "oil_gas"}),
    "real estate": frozenset({"real_estate", "diversified_financials"}),
}
_GICS_ALIASES = {
    "it": "information technology", "info tech": "information technology",
    "technology": "information technology", "tech": "information technology",
    "healthcare": "health care", "health": "health care",
    "financial": "financials", "financial services": "financials",
    "consumer cyclical": "consumer discretionary", "discretionary": "consumer discretionary",
    "consumer defensive": "consumer staples", "staples": "consumer staples",
    "communications": "communication services", "communication": "communication services",
    "telecommunication services": "communication services",
    "telecommunications": "communication services", "telecom": "communication services",
    "telecommunications services": "communication services", "comm services": "communication services",
    "basic materials": "materials", "industrial": "industrials",
    "utility": "utilities", "realestate": "real estate",
}


def gics_sector_key(name: str | None) -> str | None:
    """The canonical lowercase GICS sector for a register `sector` value, or None when it is
    empty or not one of the 11 sectors (or a common variant)."""
    if not name or not str(name).strip():
        return None
    key = re.sub(r"\s+", " ", str(name).replace("&", " and ").replace("-", " ").strip().lower())
    key = _GICS_ALIASES.get(key, key)
    return key if key in _GICS else None


def gics_clusters(name: str | None) -> frozenset[str] | None:
    key = gics_sector_key(name)
    return _GICS[key] if key else None


def gics_disagrees(gics_sector_name: str | None, cluster: str) -> bool | None:
    """True when the cluster is not compatible with the GICS sector, False when it is, None
    when the sector is empty or unrecognised (no opinion)."""
    allowed = gics_clusters(gics_sector_name)
    if allowed is None:
        return None
    return cluster not in allowed


# ── effective classification ─────────────────────────────────────────────────
def resolve(rows: Iterable[Any]) -> Any | None:
    """The effective classification among one company's rows (any objects with a `source`
    attribute or key): manual > sec_sic > extractor. None when there are no usable rows."""
    best, best_rank = None, len(SOURCE_PRECEDENCE)
    for row in rows:
        src = row["source"] if isinstance(row, dict) else row.source
        rank = SOURCE_PRECEDENCE.index(src) if src in SOURCE_PRECEDENCE else len(SOURCE_PRECEDENCE)
        if rank < best_rank:
            best, best_rank = row, rank
    return best
