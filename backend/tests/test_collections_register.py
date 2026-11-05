"""`services/collections.py::parse_register`: the collection run's target register
(JSON list or the contract's CSV), slug derivation and the briefing warnings."""
from __future__ import annotations

import pytest

from tret.services.collections import RegisterError, parse_register, register_warnings, slugify

HEADER = "company,market,domicile_country,sector,category,expected_form,fiscal_year,known_urls,notes\n"


def _csv(*rows: str) -> str:
    return HEADER + "".join(r + "\n" for r in rows)


def test_csv_happy_path_parses_every_column():
    rows, _ = parse_register(
        None,
        _csv(
            "Allianz SE,eu,Germany,Insurance,Financial,Embedded,,"
            "https://a.example/r.pdf| https://a.example/b.pdf ,check annex",
            "Apple Inc.,US,,,industrial,,2024,,",
        ),
        default_fiscal_year=2025,
    )
    assert rows[0] == {
        "company": "Allianz SE",
        "market": "EU",
        "domicile_country": "Germany",
        "sector": "Insurance",
        "category": "financial",
        "expected_form": "embedded",
        "fiscal_year": 2025,
        "known_urls": ["https://a.example/r.pdf", "https://a.example/b.pdf"],
        "notes": "check annex",
        "slug": "allianz",
        "position": 1,
    }
    assert rows[1]["slug"] == "apple"
    assert rows[1]["fiscal_year"] == 2024
    assert rows[1]["expected_form"] == "unknown"
    assert rows[1]["domicile_country"] is None and rows[1]["known_urls"] == []
    assert rows[1]["position"] == 2


def test_blank_lines_are_skipped_and_a_bom_is_tolerated():
    rows, _ = parse_register(None, "﻿" + _csv("A plc,UK,,,,,,,", ",,,,,,,,", "B plc,UK,,,,,,,"), default_fiscal_year=2025)
    assert [r["company"] for r in rows] == ["A plc", "B plc"]
    assert [r["position"] for r in rows] == [1, 2]


def test_a_bad_market_is_an_error_naming_the_row():
    with pytest.raises(RegisterError, match=r"Row 2 \(line 3\) \(Toyota\): market 'JP'"):
        parse_register(None, _csv("Shell plc,UK,,,,,,,", "Toyota,JP,,,,,,,"), default_fiscal_year=2025)


@pytest.mark.parametrize(
    ("row", "fragment"),
    [
        ("X,US,,,banking,,,,", "category 'banking'"),
        ("X,US,,,,partial,,,", "expected_form 'partial'"),
        ("X,US,,,,,20x5,,", "fiscal_year"),
        ("X,US,,,,,,http://x.example/a.pdf,", "https://"),
        (",US,,,,,,,", "company is required"),
    ],
)
def test_row_validation_errors(row, fragment):
    with pytest.raises(RegisterError, match=fragment):
        parse_register(None, _csv(row), default_fiscal_year=2025)


def test_unknown_columns_and_missing_header_columns_are_refused():
    with pytest.raises(RegisterError, match="Unknown CSV column"):
        parse_register(None, "company,market,ticker\nA,US,AAA\n", default_fiscal_year=2025)
    with pytest.raises(RegisterError, match="'market'"):
        parse_register(None, "company\nA\n", default_fiscal_year=2025)


def test_exactly_one_register_form_is_required():
    with pytest.raises(RegisterError, match="exactly one"):
        parse_register(None, None, default_fiscal_year=2025)
    with pytest.raises(RegisterError, match="exactly one"):
        parse_register([], "x", default_fiscal_year=2025)
    with pytest.raises(RegisterError, match="no targets"):
        parse_register([], None, default_fiscal_year=2025)


@pytest.mark.parametrize(
    ("company", "slug"),
    [
        ("Allianz SE", "allianz"),
        ("Shell plc", "shell"),
        ("Siemens AG", "siemens"),
        ("Apple Inc.", "apple"),
        ("Microsoft Corporation", "microsoft"),
        ("ASML Holding N.V.", "asml-holding"),
        ("Société Générale S.A.", "societe-generale"),
        ("Marks & Spencer Group plc", "marks-and-spencer-group"),
        ("A" * 60, "a" * 40),
        ("!!!", "target"),
    ],
)
def test_slugify(company, slug):
    assert slugify(company) == slug


def test_slugs_are_deduplicated_and_explicit_slugs_are_reserved():
    rows, _ = parse_register(
        [
            {"company": "Shell plc", "market": "UK"},
            {"company": "Shell PLC", "market": "UK"},
            {"company": "Other", "market": "UK", "slug": "shell-3"},
            {"company": "Shell", "market": "UK"},
        ],
        None,
        default_fiscal_year=2025,
    )
    assert [r["slug"] for r in rows] == ["shell", "shell-2", "shell-3", "shell-4"]


def test_a_duplicate_explicit_slug_is_an_error():
    with pytest.raises(RegisterError, match="used twice"):
        parse_register(
            [{"company": "A", "market": "US", "slug": "a"}, {"company": "B", "market": "US", "slug": "a"}],
            None,
            default_fiscal_year=2025,
        )


def test_json_register_accepts_url_lists_and_strings():
    rows, _ = parse_register(
        [
            {"company": "A", "market": "us", "known_urls": ["https://a.example/1.pdf"]},
            {"company": "B", "market": "us", "known_urls": "https://b.example/1.pdf|https://b.example/2.pdf"},
        ],
        None,
        default_fiscal_year=2025,
    )
    assert rows[0]["known_urls"] == ["https://a.example/1.pdf"]
    assert len(rows[1]["known_urls"]) == 2


def _row(market, category=None, form="unknown"):
    return {"market": market, "category": category, "expected_form": form}


def test_warnings_follow_the_briefing_rules():
    rows = (
        [_row("US", "financial")] * 3
        + [_row("US", "industrial")] * 5
        + [_row("UK", "financial")] * 2
        + [_row("UK", "industrial", "embedded")] * 5
        + [_row("UK", "other", "embedded")]
    )
    assert register_warnings(rows) == [
        "UK: 2 financial institutions (briefing asks for at least 3)",
        "EU: no targets",
    ]


def test_warnings_cover_industrial_and_embedded_shortfalls_and_singulars():
    rows = [_row("US", "financial")] * 3 + [_row("US", "industrial")] + [
        _row("EU", "financial", "embedded")
    ] * 3 + [_row("EU", "industrial", "standalone")] * 5 + [_row("UK", "financial", "embedded")] * 3 + [
        _row("UK", "industrial", "embedded")
    ] * 3 + [_row("UK", "industrial")] * 2
    warnings = register_warnings(rows)
    assert "US: 1 industrial company (briefing asks for at least 5)" in warnings
    assert "EU: 3 embedded disclosures (briefing asks for at least 6)" in warnings
    assert not any(w.startswith("UK") for w in warnings)


def test_parse_register_returns_the_warnings():
    _, warnings = parse_register(None, _csv("A,US,,,financial,,,,"), default_fiscal_year=2025)
    assert "US: 1 financial institution (briefing asks for at least 3)" in warnings
    assert "UK: no targets" in warnings and "EU: no targets" in warnings


def test_the_prompt_names_only_real_packaging_roles_and_the_target_year():
    from types import SimpleNamespace

    from tret.services.collections import build_prompt
    from tret.services.packaging import ROLES

    c = SimpleNamespace(run_slug="r1")
    t = SimpleNamespace(
        position=2, company="Shell plc", market="UK", domicile_country=None, sector=None,
        category=None, expected_form="embedded", fiscal_year=2024, known_urls=[], notes=None,
        slug="shell",
    )
    prompt = build_prompt(c, t, 5)
    for role in ROLES:
        assert role in prompt
    assert "embedded_sustainability_section" not in prompt and "incorporated_section" not in prompt
    assert "embedded_statement_and_assurance for the sustainability section of an annual" in prompt
    assert "primary_disclosure for a standalone sustainability report" in prompt
    assert "fy2025_gap=true when the period collected is not FY2024" in prompt
    assert "target 2 of 5" in prompt
