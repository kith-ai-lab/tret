"""R11 (code-aware number check, location detail) and R12 (small numbers with
units, arithmetic stays inside one paragraph/row)."""
from __future__ import annotations

from tret.engine.grounding import (
    evidence_numbers,
    skipped_code_blocks,
    unsupported_details,
    unsupported_numbers,
)

CSS_DECK = """Here is the deck.

```html
<!doctype html>
<html><head><style>
.slide { padding: 5%; left: 5%; right: 5%; width: 90% }
</style></head>
<body><h1>Plan</h1><p>Revenue 4,200</p></body></html>
```
"""


# ── R11 ──
def test_css_in_an_html_fence_is_not_checked_but_visible_text_is():
    assert unsupported_numbers(CSS_DECK, set()) == ["4,200"]
    assert unsupported_numbers(CSS_DECK, {"4200"}) == []


def test_attributes_and_script_are_not_checked():
    reply = '```html\n<div style="width:55%" data-x="77"><script>var n = 1234;</script>ok</div>\n```'
    assert unsupported_numbers(reply, set()) == []


def test_python_css_js_sh_sql_fences_are_skipped():
    for lang in ("python", "css", "js", "javascript", "ts", "bash", "sh", "sql", "py", "scss"):
        reply = f"```{lang}\nx = 4242\n```"
        assert unsupported_numbers(reply, set()) == [], lang


def test_json_csv_and_unlabelled_fences_are_checked():
    for lang in ("json", "csv", "text", "", "markdown"):
        reply = f"```{lang}\n{{\"score\": 4242}}\n```"
        assert unsupported_numbers(reply, set()) == ["4242"], lang


def test_inline_code_is_skipped():
    assert unsupported_numbers("Set `limit=4242` then see 4243.", set()) == ["4243"]


def test_full_html_document_reply_checks_visible_text_only():
    doc = "<!DOCTYPE html>\n<html><head><style>a{margin:12%}</style></head>\n<body>\n<p>Total 8,800</p>\n</body></html>"
    [d] = unsupported_details(doc, set())
    assert d["value"] == "8,800" and d["where"] == "html" and d["line"] == 4


def test_skipped_code_blocks_counted():
    reply = "```python\nx=1000\n```\n\n```css\na{b:50%}\n```\n```html\n<style>a{b:60%}</style>\n```"
    assert skipped_code_blocks(reply) == 3


def test_unclosed_python_fence_in_truncated_reply_is_skipped():
    assert unsupported_numbers("Intro\n```python\nx = 4242\n", set()) == []


def test_details_where_line_context():
    reply = "Intro line.\n\nPayback is 12.5 months overall.\n\n| a | b |\n|---|---|\n| 3,300 | x |\n\n- item with 7,700 in it\n"
    details = unsupported_details(reply, set())
    assert [(d["value"], d["where"], d["line"]) for d in details] == [
        ("12.5", "prose", 3),
        ("3,300", "table", 7),
        ("7,700", "list", 9),
    ]
    assert "12.5 months" in details[0]["context"] and len(details[0]["context"]) <= 120
    assert [d["value"] for d in details] == unsupported_numbers(reply, set())


def test_details_are_deduplicated_first_seen():
    assert [d["line"] for d in unsupported_details("a 4242\nb 4242\nc 5151", set())] == [1, 3]


# ── R12 ──
def test_unit_bearing_small_numbers_are_checked():
    assert unsupported_numbers("Payback is 3 yr.", set()) == ["3"]
    assert unsupported_numbers("Payback is 3 years and 4 months.", set()) == ["3", "4"]
    assert unsupported_numbers("A 4x gain and 5MW of load.", set()) == ["4", "5"]
    assert unsupported_numbers("It costs $5 per unit.", set()) == ["5"]
    assert unsupported_numbers("Share is 5%.", set()) == ["5%"]


def test_small_counts_without_units_stay_exempt():
    assert unsupported_numbers("We checked 3 datasets and 2 sites.", set()) == []
    assert unsupported_numbers("1. First\n2. Second", set()) == []


def test_small_number_with_unit_is_supported_by_evidence_including_small_ints():
    ev = evidence_numbers(system="", messages=[], task_input={"message": "payback 3 yr"})
    assert "3" in ev
    assert unsupported_numbers("Payback is 3 yr.", ev) == []


def test_bens_payback_case_not_rescued_by_arithmetic_elsewhere():
    reply = (
        "Facility inputs: 120 kWh and 245 kWh were supplied.\n\n"
        "Savings total 365 kWh across both.\n\n"
        "Paybacks are 3.0 yr and 2.5 yr."
    )
    # 365 sits in its own paragraph, so it is not derived from 120 + 245.
    out = unsupported_numbers(reply, {"120", "245", "365"})
    assert out == ["3.0", "2.5"]


def test_arithmetic_within_one_paragraph_still_works():
    assert unsupported_numbers("Inputs 120 and 245 give 365 together.", {"120", "245"}) == []


def test_arithmetic_does_not_cross_paragraphs():
    reply = "Inputs 120 and 245.\n\nTogether that is 365."
    assert unsupported_numbers(reply, {"120", "245"}) == ["365"]


def test_arithmetic_works_across_rows_of_one_table_not_other_paragraphs():
    ok = "| a | b | sum |\n|---|---|---|\n| 120 | 245 | 365 |"
    assert unsupported_numbers(ok, {"120", "245"}) == []
    cross = "| a | b |\n|---|---|\n| 120 | 245 |\n| total | 365 |"
    assert unsupported_numbers(cross, {"120", "245"}) == []  # totals row, other row
    plain = "| a | b |\n|---|---|\n| 120 | 245 |\n| x | 365 |"
    assert unsupported_numbers(plain, {"120", "245"}) == ["365"]  # not a totals row
    apart = "| a | b |\n|---|---|\n| 120 | 245 |\n\nUnrelated paragraph.\n\nTotal 365."
    assert unsupported_numbers(apart, {"120", "245"}) == ["365"]


def test_arithmetic_in_html_is_per_line():
    html = "```html\n<p>120 and 245</p>\n<p>365</p>\n```"
    assert unsupported_numbers(html, {"120", "245"}) == ["365"]
