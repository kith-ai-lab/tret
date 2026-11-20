"""R13: structural check of HTML in a reply."""
from __future__ import annotations

from tret.engine.html_check import check_reply_html, html_repair_nudge

GOOD = """<!doctype html>
<html>
<head><title>Deck</title><style>.a{padding:5%}</style></head>
<body>
<table>
<tr><th>A</th><th>B</th></tr>
<tr><td>1</td><td>2</td></tr>
</table>
<ul><li>one<li>two</ul>
<p>para<br>line<img src="x.png">
</body>
</html>
"""


def kinds(r):
    return [i["kind"] for i in r["issues"]]


def test_clean_document_is_ok():
    r = check_reply_html(GOOD)
    assert r == {"status": "ok", "blocks": 1, "fragments": 0, "issues": []}


def test_reply_without_html_is_skipped():
    assert check_reply_html("Just prose with `<b>` inline.")["status"] == "skipped"
    assert check_reply_html("```python\nprint('<div>')\n```")["status"] == "skipped"


def test_fenced_block_is_checked_and_lines_are_reply_lines():
    reply = "Here you go\n\n```html\n<body><div>\n<p>hi</p>\n```\nDone."
    r = check_reply_html(reply)
    assert r["status"] == "issues" and r["blocks"] == 1
    [issue] = r["issues"]
    assert issue["kind"] == "unclosed_tag" and issue["line"] == 4 and issue["block"] == 1


def test_stray_td_in_row_is_flagged_with_line():
    html = "<table>\n<tr><th>A</th><th>B</th></tr>\n<tr><td>1</td><td>2</td><td>3</td></tr>\n</table>"
    r = check_reply_html("```html\n<body>" + html + "\n```")
    [issue] = r["issues"]
    assert issue["kind"] == "row_cell_count" and issue["line"] == 4


def test_colspan_counts_as_cells():
    html = "<table><tr><th>A</th><th>B</th></tr>\n<tr><td colspan=2>total</td></tr></table>"
    assert check_reply_html("```html\n<body>" + html + "\n```")["status"] == "ok"


def test_cell_outside_table_and_row_outside_table():
    r = check_reply_html("```html\n<body><div><td>x</td><tr></tr></div>\n```")
    assert {"stray_cell", "stray_row"} <= set(kinds(r))


def test_unclosed_div_and_mismatched_end():
    r = check_reply_html("```html\n<body><section><div>text</section>\n```")
    assert kinds(r) == ["unclosed_tag"]
    assert "<div>" in r["issues"][0]["detail"]


def test_truncated_document_is_missing_html_end():
    cut = GOOD.split("</body>")[0]
    r = check_reply_html(cut)
    assert "missing_end_html" in kinds(r)


def test_unclosed_style_reports_unclosed():
    r = check_reply_html("```html\n<body><style>.a{color:red}\n```")
    assert "unclosed_tag" in kinds(r)


def test_void_elements_and_optional_end_tags_are_not_issues():
    html = "<ul><li>a<li>b</ul><dl><dt>t<dd>d</dl><p>x<p>y<hr><select><option>1<option>2</select>"
    assert check_reply_html("```html\n<body>" + html + "\n```")["status"] == "ok"


def test_stray_end_tag():
    r = check_reply_html("```html\n<body><div></div></span>\n```")
    assert kinds(r) == ["stray_end_tag"]


def test_two_blocks_numbered():
    reply = "```html\n<b>ok</b>\n```\n\n```html\n<body><div>\n```"
    r = check_reply_html(reply)
    assert r["blocks"] == 2 and [i["block"] for i in r["issues"]] == [2]


def test_repair_nudge_lists_issues_and_mentions_truncation():
    r = check_reply_html(GOOD.split("</body>")[0])
    msg = html_repair_nudge(r)
    assert "line" in msg and "```html" in msg and "draft_file" in msg
    ok = html_repair_nudge(check_reply_html("```html\n<body><div>\n```"))
    assert "draft_file" not in ok and "<div>" in ok


# ── fragments: short explanatory snippets are not held to document structure ──
def test_short_table_snippet_is_a_fragment_not_an_issue():
    r = check_reply_html("A row looks like:\n```html\n<tr><td>a</td><td>b</td></tr>\n```")
    assert r == {"status": "skipped", "blocks": 1, "fragments": 1, "issues": []}


def test_short_div_snippet_is_a_fragment_not_an_issue():
    r = check_reply_html("```html\n<div class=\"card\">\n```")
    assert r["status"] == "skipped" and r["fragments"] == 1 and r["issues"] == []


def test_full_document_with_a_stray_cell_is_still_flagged():
    doc = "<!DOCTYPE html>\n<html><body>\n<div><td>x</td></div>\n</body></html>"
    r = check_reply_html("```html\n" + doc + "\n```")
    assert r["status"] == "issues" and r["fragments"] == 0 and "stray_cell" in kinds(r)
    assert check_reply_html("```html\n<body><tr></tr>\n```")["status"] == "issues"


def test_forty_line_block_counts_as_a_deliverable():
    long_block = "<div>\n" + "<p>x</p>\n" * 38 + "</span>"  # 40 lines, no doctype/html/body
    assert check_reply_html("```html\n" + long_block + "\n```")["status"] == "issues"
    short_block = "<div>\n" + "<p>x</p>\n" * 37 + "</span>"  # 39 lines
    assert check_reply_html("```html\n" + short_block + "\n```")["status"] == "skipped"


def test_mixed_blocks_count_fragments_and_check_the_rest():
    reply = "```html\n<td>x</td>\n```\n```html\n<html><body><div></body></html>\n```"
    r = check_reply_html(reply)
    assert r["blocks"] == 2 and r["fragments"] == 1 and [i["block"] for i in r["issues"]] == [2]
