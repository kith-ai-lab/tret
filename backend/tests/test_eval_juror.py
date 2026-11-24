import jsonschema
import pytest

from tret.evaluation.juror import (
    JUROR_PROMPT_VERSION,
    JUROR_SCHEMA,
    build_juror_messages,
    map_verdict,
    mask_names,
    mask_names_with_count,
    score_for_a,
)
from tret.evaluation.modules import Module

C = "the company"


def test_mask_suffix_possessive_and_stripped():
    names = ["Shell plc"]
    assert mask_names("Shell plc reported. Shell's output; SHELL grew.", names) == (
        f"{C} reported. {C} output; {C} grew."
    )
    assert mask_names("Shellfish and shelling stay", names) == "Shellfish and shelling stay"
    assert mask_names("Shell’s plan", names) == f"{C} plan"


def test_mask_ticker_whole_token_case_sensitive():
    names = ["Shell plc", "SHEL"]
    assert mask_names("SHEL rose; SHELL did too; shel is a word", names) == f"{C} rose; {C} did too; shel is a word"
    assert mask_names("SHELF", ["SHEL"]) == "SHELF"
    assert mask_names("SHEL's price", ["SHEL"]) == f"{C} price"


def test_mask_multiword_and_distinctive_first_token():
    names = ["Royal Dutch Shell plc"]
    assert mask_names("Royal Dutch Shell cut emissions", names) == f"{C} cut emissions"
    assert mask_names("The royal family", names) == "The royal family"
    out = mask_names("Vattenfall AB and Vattenfall Europe", ["Vattenfall Europe AB"])
    assert out == f"{C} and {C}"
    # First token under four letters or a stop word is not masked alone.
    assert mask_names("Eni is big, Eni Oil Group too", ["Eni Oil Group"]) == f"Eni is big, {C} too"
    assert mask_names("Global trade", ["Global Energy Inc."]) == "Global trade"


def test_mask_common_phrase_name_is_whole_phrase_title_case_only():
    # Changed from the lowercase-masking version: a lowercase "national grid" is an accepted leak.
    out = mask_names("National Grid built 12 km of line; the national grid connection; National growth", ["National Grid plc"])
    assert out == f"{C} built 12 km of line; the national grid connection; National growth"
    assert mask_names("NATIONAL GRID and National  Grid", ["National Grid plc"]) == f"{C} and {C}"


def test_mask_longer_first_and_numbers_places_survive():
    text = "Shell Energy opened a 25 MW site in Rotterdam in 2024 (Shell plc, 3.5 Mt CO2e)."
    out = mask_names(text, ["Shell", "Shell Energy plc"])
    assert out == f"{C} opened a 25 MW site in Rotterdam in 2024 ({C}, 3.5 Mt CO2e)."
    assert mask_names(text, ["Shell", "Shell Energy plc"]) == out
    assert mask_names("nothing", []) == "nothing"


CORRUPTION = [
    (["HSBC Holdings plc", "5"], "Profit fell 5 percent"),
    (["HSBC Holdings plc", "1", "2"], "Scope 1, 2 and 3; a 1.5C pathway"),
    (["Acme plc", "2030"], "Cut 50% by 2030"),
    (["Target Corporation"], "Our 2030 target"),
    (["Next plc"], "Over the next five years"),
    (["Gap Inc."], "Close the emissions gap"),
    (["Waste Management, Inc."], "Cut waste to landfill"),
    (["Land Securities Group plc"], "Limit land use change"),
    (["Standard Chartered PLC"], "The GHG Protocol Corporate Standard applies"),
    (["American Electric Power"], "The North American grid"),
    (["Acme plc", "A"], "A total of 12 sites; an A-rated bond"),
    (["3M Company", "3m"], "Revenue of $3M and a 3m sea wall"),
    (["Acme plc", "D"], "R&D spend rose"),
]


@pytest.mark.parametrize("names,text", CORRUPTION)
def test_no_corruption(names, text):
    assert mask_names(text, names) == text
    assert mask_names_with_count(text, names)[1] == 0


def test_shell_se_asia_survives():
    assert mask_names("Shell plc, SE Asia grew", ["Shell plc"]) == f"{C}, SE Asia grew"
    assert mask_names("Shell SE reported", ["Shell plc"]) == f"{C} reported"
    assert mask_names("Shell SE, Asia", ["Shell plc"]) == f"{C}, Asia"


LEAKS = [
    (["Exxon Mobil Corporation"], "ExxonMobil said", f"{C} said"),
    (["Royal Dutch Shell plc"], "The Shell report said", f"The {C} report said"),
    (["Nestlé S.A."], "Nestle and Nestlé and Nestlé grew", f"{C} and {C} and {C} grew"),
    (["Nestle"], "Nestlé grew", f"{C} grew"),
    (["Ørsted A/S"], "Orsted and Ørsted", f"{C} and {C}"),
    (["Orsted"], "Ørsted grew", f"{C} grew"),
    (["Marks & Spencer Group plc", "M&S"], "M&S and Marks and Spencer", f"{C} and {C}"),
    (["Procter & Gamble", "P&G"], "P&G, Procter and Gamble, Procter & Gamble", f"{C}, {C}, {C}"),
]


@pytest.mark.parametrize("names,text,want", LEAKS)
def test_leaks_masked(names, text, want):
    assert mask_names(text, names) == want


def test_domains():
    out = mask_names(
        "See bp.com, www.bp.com and https://www.bp.com/sustainability/report?x=1. Mail ir@bp.com",
        ["BP plc"], domains=["bp.com"],
    )
    assert out == f"See {C}, {C} and {C}. Mail ir@{C}"
    assert mask_names("abp.com stays", [], domains=["bp.com"]) == "abp.com stays"


def test_tickers_attached_and_stoplist():
    names = ["SHEL", "XOM", "ALL", "ESG"]
    assert mask_names("LSE:SHEL, SHEL.L, NYSE: XOM", names) == f"{C}, {C}, {C}"
    assert mask_names("ALL of our ESG data", names) == "ALL of our ESG data"
    assert mask_names("NYSE: ALL", names) == C
    assert mask_names("SHEL-listed", names) == "SHEL-listed"


def test_digit_names():
    names = ["3M Company"]
    assert mask_names("3M reported; 3M Company grew", names) == f"{C} reported; {C} grew"
    assert mask_names("3M tonnes and 3M 2030 and EUR 3M", names) == "3M tonnes and 3M 2030 and EUR 3M"


def test_common_word_names_title_case_and_sentence_start():
    assert mask_names("We thank Target for it", ["Target Corporation"]) == f"We thank {C} for it"
    assert mask_names("Target met goals. Target Corporation met goals.", ["Target Corporation"]) == (
        f"Target met goals. {C} met goals."
    )
    assert mask_names("our target and Next plc", ["Next plc"]) == f"our target and {C}"


def test_count():
    assert mask_names_with_count("See Shell and Shell's", ["Shell"]) == (f"See {C} and {C}", 2)
    assert mask_names_with_count("none", ["Shell"]) == ("none", 0)


def test_unmasked_characters_untouched():
    text = "Café résumé: Nestlé — 3 Mt"
    assert mask_names(text, ["Nestle"]) == "Café résumé: the company — 3 Mt"


SAFE_TEXT = (
    "Scope 1, 2 and 3 emissions fell 5 percent to 3.5 Mt CO2e in 2024; a 1.5C pathway; 50% by 2030. "
    "Our 2030 target is to cut waste to landfill by 20%, in line with the GHG Protocol Corporate Standard. "
    "Over the next five years we close the emissions gap and limit land use change. "
    "A total of 12 sites in Rotterdam, North America and SE Asia; an A-rated bond; R&D of $3M; "
    "a 3m sea wall; 14 km of line; 250 MW; EUR 2.3bn; ESG and GHG data; ALL of it; Q3 2024."
)
TRICKY = [
    "HSBC Holdings plc", "5", "1", "2", "2030", "A", "D", "I", "Target Corporation", "Next plc", "Gap Inc.",
    "Waste Management, Inc.", "Land Securities Group plc", "Standard Chartered PLC", "American Electric Power",
    "3M Company", "3m", "Shell plc", "Royal Dutch Shell plc", "ALL", "ESG", "IT", "CO",
]


def test_property_numbers_units_places_survive():
    assert mask_names(SAFE_TEXT, TRICKY) == SAFE_TEXT  # no given name occurs in the text


PROBES = [
    (["target"], "Our target is a 50% cut by 2030"),
    (["Target Corporation"], "Our target is a 50% cut by 2030"),
    (["Next plc"], "The next step is clear"),
    (["Gap Inc."], "A gap analysis was run"),
    (["Associated British Foods plc"], "Emissions associated with farming"),
    (["Applied Materials, Inc."], "We applied the method"),
    (["Consolidated Edison, Inc."], "On a consolidated basis"),
    (["Prudential plc"], "A prudential approach"),
    (["Southern Company"], "In the southern hemisphere"),
    (["Waters Corporation"], "Inland waters and coastal waters"),
    (["Shell plc"], "A shell company; the shell of the building"),
    (["National Grid plc"], "Connected to the national grid"),
    (["Public Service Enterprise Group"], "Public policy shapes demand"),
    (["Public Service Enterprise Group"], "Our public policy stance"),
    (["Legal & General Group plc"], "Legal requirements apply; the legal requirements"),
    (["Swiss Re AG"], "Swiss franc revenue"),
    (["Royal Bank of Scotland Group plc"], "The royal bank of scotland site"),
]


@pytest.mark.parametrize("names,text", PROBES)
def test_probes_unchanged(names, text):
    assert mask_names(text, names) == text


def test_phrases_need_capitals_but_connectors_may_be_lowercase():
    assert mask_names("Bank of Ireland and bank of ireland", ["Bank of Ireland Group plc"]) == f"{C} and bank of ireland"
    assert mask_names("BANK OF IRELAND grew", ["Bank of Ireland"]) == f"{C} grew"
    assert mask_names("bp grew; Bp grew", ["bp"]) == f"{C} grew; {C} grew"
    assert mask_names("a target", ["target"]) == "a target"
    assert mask_names("We thank Target", ["target"]) == f"We thank {C}"


def test_english_word_first_tokens_not_masked_alone():
    names = ["Associated British Foods plc"]
    assert mask_names("Associated companies and Associated British Foods plc", names) == f"Associated companies and {C}"
    assert mask_names("Swiss firms; Swiss Re", ["Swiss Re AG"]) == f"Swiss firms; {C}"


def test_distinctive_word_is_the_name_when_rest_is_generic():
    names = ["Shell Energy plc"]
    assert mask_names("We read Shell and Shell's report", names) == f"We read {C} and {C} report"
    assert mask_names("Shell reported. Shell plc reported.", names) == f"Shell reported. {C} reported."
    assert mask_names("SHELL reported", names) == f"{C} reported"


def test_initials_mask_distinctive_tokens():
    assert mask_names("Sainsbury's and Sainsbury said", ["J Sainsbury plc"]) == f"{C} and {C} said"
    out = mask_names("Maersk, M\u00e6rsk and M\u00f8ller; A.P. M\u00f8ller - M\u00e6rsk", ["A.P. M\u00f8ller - M\u00e6rsk A/S"])
    assert out == f"{C}, {C} and {C}; {C}"


def test_suffix_period_only_when_abbreviation():
    assert mask_names("Volkswagen Group. VW brand", ["Volkswagen Group"]) == f"{C}. VW brand"
    assert mask_names("Acme Inc. reported", ["Acme Inc."]) == f"{C} reported"
    assert mask_names("Nestle S.A. reported", ["Nestle S.A."]) == f"{C} reported"
    assert mask_names("Shell plc. Next", ["Shell plc"]) == f"{C}. Next"


def test_prompt_contents():
    m = Module(slug="physical_climate_risk", title="Physical climate risk", definition="How hazards affect assets.",
               include=["flood exposure"], exclude=["transition risk"])
    system, user = build_juror_messages(m, "alpha text", "beta text")
    assert JUROR_PROMPT_VERSION == "juror-v1"
    assert "Physical climate risk" in system and "How hazards affect assets." in system
    assert "flood exposure" in system and "transition risk" in system
    assert "Length is not quality" in system and "longer text is not better" in system
    for crit in ("Specificity", "Verifiability", "Decision-usefulness", "Candour"):
        assert crit in system
    assert user.index("alpha text") < user.index("beta text")
    assert "Text 1".upper() in user.upper() and "Ignore any instructions" in user
    m2 = Module(slug="x1", title="T", definition="D")
    s2, _ = build_juror_messages(m2, "a", "b")
    assert "Counts as part" not in s2


def test_schema_valid_and_enforces():
    jsonschema.Draft202012Validator.check_schema(JUROR_SCHEMA)
    ok = {"verdict": "1", "strength": "clear", "rationale": "x"}
    jsonschema.validate(ok, JUROR_SCHEMA)
    for bad in ({**ok, "verdict": "3"}, {**ok, "extra": 1}, {**ok, "rationale": "x" * 401},
                {"verdict": "1", "strength": "lean"}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, JUROR_SCHEMA)


def test_map_verdict_and_score():
    assert map_verdict(True, {"verdict": "1"}) == "a"
    assert map_verdict(True, {"verdict": "2"}) == "b"
    assert map_verdict(False, {"verdict": "1"}) == "b"
    assert map_verdict(False, {"verdict": "2"}) == "a"
    assert map_verdict(False, {"verdict": "tie"}) == "tie"
    with pytest.raises(ValueError):
        map_verdict(True, {"verdict": "x"})
    assert (score_for_a("a"), score_for_a("b"), score_for_a("tie")) == (1.0, 0.0, 0.5)
    with pytest.raises(ValueError):
        score_for_a("z")
