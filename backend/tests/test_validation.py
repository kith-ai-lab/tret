"""The cited-values cross-check and schema validation — the mechanical heart
of trust rule #1."""
import json
from pathlib import Path

from tret.engine.validation import validate_cited_values, validate_payload

SCHEMA = json.loads(
    (Path(__file__).parent.parent.parent / "packs/climate-risk/schemas/divergence_verdict.schema.json").read_text()
)

RETRIEVED = [
    {"dataset": "hazard_scores", "row_ref": "hazard_scores:4", "column": "rating", "value": "low"},
    {"dataset": "hazard_scores", "row_ref": "hazard_scores:4", "column": "vintage_year", "value": "2018"},
    {"dataset": "regional_signals", "row_ref": "regional_signals:8", "column": "direction", "value": "increase"},
]


def _payload(**over):
    base = {
        "verdict": "diverge_signal_higher",
        "reason_code": "outdated_inputs",
        "confidence": "high",
        "methodology_note": "x" * 250,
        "cited_values": [
            {"dataset": "hazard_scores", "row_ref": "hazard_scores:4", "value": "2018"},
            {"dataset": "regional_signals", "row_ref": "regional_signals:8", "value": "increase"},
        ],
        "doctrine_citations": ["Divergence Assessment Procedure"],
    }
    base.update(over)
    return base


def test_valid_payload_passes():
    assert validate_payload(_payload(), SCHEMA) == []


def test_divergence_requires_reason_code():
    p = _payload()
    del p["reason_code"]
    errors = validate_payload(p, SCHEMA)
    assert any("reason_code" in e for e in errors)


def test_agree_needs_no_reason_code():
    p = _payload(verdict="agree")
    del p["reason_code"]
    assert validate_payload(p, SCHEMA) == []


def test_short_note_rejected():
    errors = validate_payload(_payload(methodology_note="too short"), SCHEMA)
    assert any("methodology_note" in e for e in errors)


def test_cited_value_must_be_retrieved():
    cited = [{"dataset": "hazard_scores", "row_ref": "hazard_scores:4", "value": "9999"}]
    errors = validate_cited_values(cited, RETRIEVED)
    assert len(errors) == 1 and "never retrieved" in errors[0]


def test_cited_value_wrong_row_ref_flagged():
    cited = [{"dataset": "hazard_scores", "row_ref": "hazard_scores:99", "value": "2018"}]
    errors = validate_cited_values(cited, RETRIEVED)
    assert len(errors) == 1 and "row_ref" in errors[0]


def test_exact_citation_passes():
    cited = [{"dataset": "hazard_scores", "row_ref": "hazard_scores:4", "value": "2018"}]
    assert validate_cited_values(cited, RETRIEVED) == []


def test_citation_with_the_right_column_passes():
    cited = [
        {
            "dataset": "hazard_scores",
            "row_ref": "hazard_scores:4",
            "column": "vintage_year",
            "value": "2018",
        }
    ]
    assert validate_cited_values(cited, RETRIEVED) == []


def test_value_attributed_to_the_wrong_column_is_flagged():
    """A real value from a real row, pointed at the wrong field of it.

    The cross-check ignored `column`, so "the flood rating is 2018" passed as a
    grounded citation: the number was retrieved, the row was retrieved, and only
    the field it belonged to was wrong — which is precisely the part a reader
    cannot check without going back to the dataset.
    """
    cited = [
        {
            "dataset": "hazard_scores",
            "row_ref": "hazard_scores:4",
            "column": "rating",  # 2018 is the vintage year, not the rating
            "value": "2018",
        }
    ]
    errors = validate_cited_values(cited, RETRIEVED)
    assert len(errors) == 1
    assert "value of column ['vintage_year'], not 'rating'" in errors[0]


def test_a_value_held_by_two_columns_may_cite_either():
    retrieved = [
        *RETRIEVED,
        {
            "dataset": "regional_signals",
            "row_ref": "regional_signals:8",
            "column": "magnitude_class",
            "value": "increase",
        },
    ]
    for column in ("direction", "magnitude_class"):
        cited = [
            {
                "dataset": "regional_signals",
                "row_ref": "regional_signals:8",
                "column": column,
                "value": "increase",
            }
        ]
        assert validate_cited_values(cited, retrieved) == []
