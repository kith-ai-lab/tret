"""The deterministic claim counter (tret/evaluation/density.py): one test per category, the
precedence rules (a token is counted once), the structural noise it ignores and a realistic
paragraph with an exact breakdown. Pure functions, no database."""
from __future__ import annotations

import pytest

from tret.evaluation.density import CATEGORIES, DENSITY_VERSION, claim_density
from tret.evaluation.extract import words


def counts(text: str) -> dict[str, int]:
    return {k: v for k, v in claim_density(text).counts.items() if v}


# ── categories ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "€12.5 million", "$3 billion", "£400k", "US$ 5m", "USD 2.3bn", "EUR 1,200", "SEK 450 million",
    "50 million euros", "12 USD", "¥1,000", "CHF 3.5 mn", "A$7 million",
])
def test_currency_amounts_are_one_claim(text):
    assert counts(text) == {"currency": 1}


@pytest.mark.parametrize("text", [
    "31 December 2024", "December 31, 2024", "2024-12-31", "31/12/2024", "31.12.24", "March 2025", "Sept. 2023",
    "1st of May 2020", "Q3 2024", "Q1 FY2025", "H1 2025", "FY2024", "FY24", "FY2024/25", "in 1999", "by 2050", "2100",
])
def test_dates_are_one_claim(text):
    assert counts(text) == {"date": 1}


@pytest.mark.parametrize("text", [
    "42%", "42 %", "12 percent", "3 percentage points", "50 basis points", "1,234,567 tCO2e", "12 ktCO2e", "3.4 MtCO2e",
    "5 tCO2-e", "5 t CO2e", "9 tonnes of CO2e", "120 CO₂e", "0.4 kgCO2e/kWh", "300 GWh", "12 MW", "3 GW", "40 kWh",
    "3 m³", "10 m3", "2 million litres", "800 ML", "120 ha", "45 hectares", "12 km", "1.5°C", "2 ºC", "450 ppm", "7 tonnes",
    "3.2 million tonnes", "5-10%", "5–10 %",
])
def test_quantities_with_units_are_one_claim(text):
    assert counts(text) == {"unit": 1}


@pytest.mark.parametrize("text,n", [
    ("14 suppliers", 1), ("3.5 audits", 1), ("1,234 employees", 1), ("1.234.567 items", 1), ("12 and 40 staff", 2),
    ("5-10 sites", 1), ("12,5 stores", 1),
])
def test_plain_numbers(text, n):
    assert counts(text) == {"number": n}


@pytest.mark.parametrize("text", [
    "Rotterdam refinery", "the Rotterdam refinery", "The Pilbara mine", "Port Hedland terminal", "North Sea data centre",
    "Gigafactory Berlin", "the Port of Rotterdam", "Antwerp plants",
])
def test_named_sites_are_one_claim(text):
    assert counts(text) == {"site": 1}


@pytest.mark.parametrize("text", ["Our refinery", "The plant", "Each site", "This facility", "a new plant", "the mine"])
def test_generic_site_phrases_are_not_claims(text):
    assert counts(text) == {}


def test_sites_each_count_once():
    assert counts("The Rotterdam refinery, the Antwerp plant and Hamburg terminal") == {"site": 3}


# ── precedence: a token is counted once ─────────────────────────────────────────────────────
def test_precedence_one_token_one_category():
    assert counts("€12.5 million") == {"currency": 1}
    assert counts("42% by 2030") == {"unit": 1, "date": 1}
    assert counts("1,234,567 tCO2e") == {"unit": 1}
    assert counts("Q3 2024") == {"date": 1}  # not a date plus a number
    assert counts("31 December 2024") == {"date": 1}
    assert counts("1.2 million tonnes of waste at the Antwerp plant") == {"unit": 1, "site": 1}


def test_a_year_followed_by_a_unit_is_a_quantity_not_a_date():
    assert counts("2025 tCO2e") == {"unit": 1}
    assert counts("2025 tCO2e in 2025") == {"unit": 1, "date": 1}


def test_ranges_of_years_are_two_dates():
    assert counts("2020-2030") == {"date": 2}


# ── noise ───────────────────────────────────────────────────────────────────────────────────
def test_page_and_section_markers_are_ignored():
    assert counts("## Page 12\n\n## Section 3: Environment\n") == {}
    assert counts("## Page 12\n42% of sites") == {"unit": 1}


def test_heading_numbering_and_list_markers_are_ignored():
    assert counts("1.2.3 Governance\n") == {}
    assert counts("## 4.1 Targets\n") == {}
    assert counts("1. Introduction\n2) Scope of work\n(a) first point\n(iv) fourth point\n- (b) second") == {}
    # a decimal at the start of a line followed by a lower-case word is a quantity, not numbering
    assert counts("3.5 million tonnes were recycled") == {"unit": 1}


def test_footnote_markers_and_superscripts_are_ignored():
    assert counts("Emissions fell[12] last year[3].") == {}
    assert counts("Emissions¹ fell² ³") == {}
    assert counts("Use fell to 3 m³ per tonne") == {"unit": 1}  # the exponent of m³ is part of the unit


def test_cross_references_are_not_claims():
    assert counts("See Table 4, Figure 2 and Section 3.1 on page 14 (p. 12, pp. 12-14).") == {}
    assert counts("Scope 1, 2 and 3 emissions") == {}
    assert counts("Aligned with ISO 14001, GRI 305-1, ESRS E1, IFRS S2 and Article 8(1) of the Regulation") == {}
    assert counts("SDG 13 and Principle 7") == {}


def test_urls_are_ignored():
    assert counts("Details at https://example.com/report-2024/page-12?x=5 and www.example.org/2023") == {}


# ── edge cases ──────────────────────────────────────────────────────────────────────────────
def test_empty_and_wordless_text():
    for t in ("", "   \n\t", "## Page 3\n"):
        d = claim_density(t)
        assert d.claims == 0 and d.per_100_words == 0.0
        assert set(d.counts) == set(CATEGORIES) and not any(d.counts.values())
    assert claim_density("").words == 0
    assert claim_density("## Page 3\n\n").per_100_words == 0.0


def test_words_match_the_extractor_word_count():
    t = "## Page 1\nWe cut 42% of emissions at the Antwerp plant.\n\n| a | b |\n"
    assert claim_density(t).words == words(t)


def test_deterministic_and_versioned():
    t = "FY2024: €12.5 million at the Rotterdam refinery, 42% by 2030, 14 audits."
    a, b = claim_density(t), claim_density(t)
    assert a == b and a.version == DENSITY_VERSION == "claims-1"


def test_per_100_words_rounds_to_two_places():
    d = claim_density("42% of the 14 sites, say so")  # 2 claims, 7 words
    assert d.claims == 2 and d.words == 7 and d.per_100_words == round(200 / 7, 2) == 28.57


def test_numbers_inside_words_and_ordinals_are_not_counted():
    assert counts("CO2 emissions of our B2B unit, the 3rd quarter, 2x faster") == {}


# ── a realistic paragraph, exact breakdown ──────────────────────────────────────────────────
PARAGRAPH = """## Page 14
1.2.3 Physical climate risk
(a) In FY2024 we spent €12.5 million on flood defences at the Rotterdam refinery and our Antwerp plant [12].
(b) Scope 1 and 2 emissions fell 42% to 1,234,567 tCO2e against a 2019 baseline; see Table 4.
(c) We target net zero by 2050 across 14 sites, with 3 hubs in Q3 2026 at a cost of USD 2.3bn.
"""


def test_realistic_paragraph_exact_breakdown():
    d = claim_density(PARAGRAPH)
    assert d.counts == {"currency": 2, "date": 4, "unit": 2, "number": 2, "site": 2}
    assert d.claims == 12
    assert d.words == words(PARAGRAPH)
    assert d.per_100_words == round(12 * 100 / d.words, 2)
    assert d.per_100_words == 18.18 and d.words == 66


# ── review fixes ────────────────────────────────────────────────────────────────────────────
PATHOLOGICAL = [
    "A-", "Ab-", "A'", "A’", "Ab-Cd ", "Ab ", "1,", "1.", "1.1.", "1 ", "€1 ", "Q1 ", "Scope 1, ", "Scope 1 and ",
    "Table 1-", "1-1-", "1/1/", "\n1. ", "Port of ", "Gigafactory ", "The ", " tCO2", "5 m ", "(a) ", "1.2.3 ", "ISO 1:",
]


@pytest.mark.parametrize("unit", PATHOLOGICAL)
def test_a_200k_character_pathological_text_is_counted_in_under_a_second(unit):
    import time

    text = unit * (200_000 // len(unit))
    start = time.perf_counter()
    claim_density(text)
    assert time.perf_counter() - start < 1.0


def test_site_names_start_at_a_token_start():
    assert counts("Port-Hedland terminal, O'Neill plant and the Rotterdam refinery") == {"site": 3}
    assert counts("x-Rotterdam refinery") == {}  # a name does not start in the middle of a token


def test_a_reference_does_not_swallow_the_claim_after_it():
    assert counts("Scope 2, 3,400 tCO2e") == {"unit": 1}
    assert counts("Section 4, 2024 targets") == {"date": 1}
    assert counts("Scope 1 and 2 – 50% lower") == {"unit": 1}
    assert counts("Goals 2030") == {"date": 1}
    assert counts("Scope 1-3 emissions and Scope 1, 2 and 3 emissions") == {}  # real references still go
    assert counts("ISO 14001:2015") == {}


def test_two_level_numbers_at_line_start_are_quantities():
    assert counts("2.5 Million tonnes were recycled") == {"unit": 1}
    assert counts("1.5 (2023: 1.2)") == {"number": 2, "date": 1}
    assert counts("1.2.3 Governance\n## 4.1 Targets\n") == {}  # numbering is still dropped


def test_a_line_initial_page_reference_is_not_a_list_marker():
    assert counts("p. 12 of the report") == {}
    assert counts("p. 12\npp. 3-5") == {}


def test_lowercase_scope_and_category_are_references():
    assert counts("scope 3 emissions and category 11") == {}
    assert counts("Scope 3 and category 11 and iso 5 sites") == {"number": 1}  # standard names stay case-sensitive
