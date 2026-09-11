"""The prose grounding check: number extraction, evidence, and the diff.

Offline and pure — no database, no provider, no engine loop. The loop's own
side of the contract (one nudge per bad reply, the budget, `run.grounding`) is
covered by the golden-run scenarios in tests/evals/test_engine_loop.py.
"""
from __future__ import annotations

from tret.engine.grounding import (
    GROUNDING_MAX_REPAIRS,
    evidence_numbers,
    extract_numbers,
    run_has_retrieval_evidence,
    unsupported_numbers,
)
from tret.providers.base import Msg, ToolCall
from tret.services.transcript import ENGINE_NUDGE_KEY, NUDGE_EMPTY_REPLY


# ── extract_numbers ─────────────────────────────────────────────────────────
def test_a_plain_number_is_extracted():
    assert extract_numbers("the score is 58") == {"58"}


def test_thousands_commas_and_bare_digits_compare_equal():
    assert extract_numbers("1,234 rows") == extract_numbers("1234 rows") == {"1234"}


def test_trailing_zeros_compare_equal():
    assert extract_numbers("71") == extract_numbers("71.0") == extract_numbers("71.00") == {"71"}


def test_percent_sign_is_stripped_for_comparison():
    assert extract_numbers("a 71% score") == extract_numbers("a score of 71") == {"71"}


def test_a_number_glued_to_a_letter_or_hyphen_is_not_matched():
    assert extract_numbers("site S-011 in R-VALLEY") == set()


def test_a_letter_coded_hazard_id_never_matches_even_with_a_trailing_number():
    # "COVID-19" is a code, not a claimed figure: the digits are preceded by
    # "<letter>-", which the range exemption below deliberately still excludes.
    assert extract_numbers("per COVID-19 guidance") == set()


def test_a_numeric_range_yields_both_operands():
    # Unlike a hazard code's suffix, a range's second number is preceded by
    # "<digit>-", not "<letter>-" — the two lookbehinds tell them apart.
    assert extract_numbers("a range of 40-58") == {"40", "58"}


def test_a_year_range_yields_both_operands():
    assert extract_numbers("spanning 2019-2021") == {"2019", "2021"}


def test_a_dollar_amount_is_still_matched():
    assert extract_numbers("costs $58 today") == {"58"}


def test_a_trailing_comma_is_not_part_of_the_value():
    # "score 58, rating moderate" — the comma is sentence punctuation, not a
    # thousands separator, even though `[\d,]*` alone cannot tell the two apart.
    assert extract_numbers("score 58, rating moderate") == {"58"}


def test_markdown_list_ordinals_are_exempt():
    text = "1. First point\n2) Second point\nthe risk score is 58"
    assert extract_numbers(text) == {"58"}


def test_a_number_that_is_also_a_list_ordinal_elsewhere_still_counts():
    # The exemption is per-occurrence, not per-value: "58" as a citation later
    # in the text still counts even though "58." opened a list item.
    text = "58. Opening point\nthe retrieved score was 58"
    assert extract_numbers(text) == {"58"}


def test_small_bare_integers_are_exempt():
    assert extract_numbers("we checked 3 datasets and found 0 matches") == set()


def test_small_integers_with_a_decimal_point_are_not_exempt():
    assert extract_numbers("a factor of 3.0") == {"3"}


def test_ten_and_above_is_never_exempt():
    assert extract_numbers("10 datasets, 9 matches") == {"10"}


def test_no_negative_numbers_are_matched():
    assert extract_numbers("a change of -5 units") == set()


def test_empty_text_extracts_nothing():
    assert extract_numbers("") == set()
    assert extract_numbers(None) == set()  # type: ignore[arg-type]


def test_an_iso_date_yields_nothing():
    # A date is not a claimed figure: with the range exemption above letting
    # "2019-2021" through as two numbers, an ISO date needs its own explicit
    # exclusion or "2026-09-11" would now extract "2026" and "11" (and drop
    # "09" only by coincidence of the small-integer exemption).
    assert extract_numbers("filed on 2026-09-11") == set()


def test_a_real_number_is_unaffected_by_the_iso_date_exclusion():
    # Only digits actually inside the \d{4}-\d{2}-\d{2} span are excluded — a
    # number elsewhere in the same sentence, even one that looks date-ish on
    # its own (a bare year), is still extracted normally.
    assert extract_numbers("filed on 2026-09-11 with a budget of 500") == {"500"}


def test_a_decimal_followed_by_a_unit_letter_is_extracted_without_it():
    # The old single-branch regex backtracked off the decimal point entirely
    # rather than accept a glued unit letter, silently dropping "1.5" from
    # "1.5C" instead of extracting it.
    assert extract_numbers("a rise of 1.5C by 2100") == {"1.5", "2100"}


def test_a_duration_adjective_does_not_swallow_the_hyphen_word():
    # "30-year" is not a range (the word after the hyphen isn't a number) and
    # not a glued unit letter (a letter never follows a bare integer) — just
    # an ordinary integer followed by an unrelated hyphenated word.
    assert extract_numbers("a 30-year mortgage") == {"30"}


def test_a_bare_reference_number_is_unaffected_by_the_new_branches():
    assert extract_numbers("ISO 27001 compliant") == {"27001"}


# ── evidence_numbers ─────────────────────────────────────────────────────────
def test_evidence_includes_the_system_prompt():
    evidence = evidence_numbers(system="the threshold is 40", messages=[], task_input={})
    assert "40" in evidence


def test_evidence_includes_tool_results_errors_included():
    # A tool error message's own numbers are ordinarily evidence — but not
    # when the only reason the number appears is that the tool echoed back
    # the model's own call arguments. `lookup_dataset`'s "No rows ... match
    # {filters}" does exactly this: without the tool_call_id subtraction, a
    # model could put any number it liked into `filters` and have this
    # message launder it into "something this run retrieved".
    messages = [
        Msg(
            role="assistant",
            content="looking it up",
            tool_calls=[ToolCall(id="tc1", name="lookup_dataset", arguments={"filters": {"site_id": 12}})],
        ),
        Msg(
            role="tool",
            content="No rows in 'hazard_scores' match {'site_id': 12}",
            tool_call_id="tc1",
            meta={"error": True},
        ),
    ]
    evidence = evidence_numbers(system="", messages=messages, task_input={})
    assert "12" not in evidence


def test_a_tool_results_own_numbers_beyond_the_echoed_arguments_are_still_evidence():
    # The subtraction removes exactly the call's own argument numbers, not
    # every number in the result — a genuinely different figure the tool
    # states alongside the echo (e.g. a row count) is untouched.
    messages = [
        Msg(
            role="assistant",
            content="looking it up",
            tool_calls=[ToolCall(id="tc1", name="lookup_dataset", arguments={"filters": {"site_id": 12}})],
        ),
        Msg(
            role="tool",
            content="No rows in 'hazard_scores' match {'site_id': 12}. 40 rows total in dataset.",
            tool_call_id="tc1",
        ),
    ]
    evidence = evidence_numbers(system="", messages=messages, task_input={})
    assert "12" not in evidence
    assert "40" in evidence


def test_only_the_matching_tool_calls_own_arguments_are_subtracted():
    # A different call's arguments (matched by a different tool_call_id) must
    # not scrub a number a DIFFERENT tool result happens to also contain.
    messages = [
        Msg(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="tc1", name="lookup_dataset", arguments={"filters": {"site_id": 12}})],
        ),
        Msg(role="tool", content="12 rows found", tool_call_id="tc2"),
    ]
    evidence = evidence_numbers(system="", messages=messages, task_input={})
    assert "12" in evidence


def test_a_genuinely_retrieved_value_is_evidence_via_the_retrieved_argument():
    # `retrieved` is `ctx.retrieved_values` — the registry engine/validation.py
    # already trusts for the cited-values cross-check. A row's real value is
    # still evidence even though the tool-result echo of the filter that
    # matched it was just scrubbed above.
    evidence = evidence_numbers(
        system="", messages=[], task_input={}, retrieved=[{"dataset": "hazard_scores", "value": "58"}]
    )
    assert "58" in evidence


def test_evidence_includes_ordinary_user_messages():
    messages = [Msg(role="user", content="my budget is 500")]
    evidence = evidence_numbers(system="", messages=messages, task_input={})
    assert "500" in evidence


def test_engine_nudges_are_excluded_from_evidence():
    messages = [
        Msg(
            role="user",
            content="You returned no text (attempt 99)",
            meta={ENGINE_NUDGE_KEY: NUDGE_EMPTY_REPLY},
        )
    ]
    evidence = evidence_numbers(system="", messages=messages, task_input={})
    assert "99" not in evidence


def test_the_wire_only_budget_line_is_excluded_from_evidence():
    messages = [Msg(role="user", content="context used: 4000 tokens", meta={"budget_line": True})]
    evidence = evidence_numbers(system="", messages=messages, task_input={})
    assert "4000" not in evidence


def test_no_assistant_turn_in_messages_is_evidence_not_even_an_earlier_one():
    # A rejected earlier draft (from a grounding nudge and rewrite, in the
    # same run) must not become evidence for the number it fabricated —
    # otherwise a rewrite could just repeat the same fabricated figure and
    # pass the check by "citing" its own rejected prior turn.
    messages = [
        Msg(role="assistant", content="earlier I mentioned 17"),
        Msg(role="assistant", content="the reply under test cites 58"),
    ]
    evidence = evidence_numbers(system="", messages=messages, task_input={})
    assert "17" not in evidence
    assert "58" not in evidence


def test_evidence_includes_task_input_message():
    evidence = evidence_numbers(
        system="", messages=[], task_input={"message": "my site is at 350 feet"}
    )
    assert "350" in evidence


def test_evidence_includes_only_user_role_history():
    # A prior run's assistant reply can itself have shipped `unresolved` —
    # still carrying a number nothing ever supported, because the grounding
    # budget was exhausted rather than the number retracted. Counting it as
    # evidence for the next turn would let that fabrication launder itself
    # across turns instead of across rewrites within one run.
    task_input = {
        "_history": [
            {"role": "user", "content": "the threshold was 12"},
            {"role": "assistant", "content": "confirmed, using 999"},
        ]
    }
    evidence = evidence_numbers(system="", messages=[], task_input=task_input)
    assert "12" in evidence
    assert "999" not in evidence


# ── unsupported_numbers ──────────────────────────────────────────────────────
def test_a_number_absent_from_evidence_is_unsupported():
    assert unsupported_numbers("the score is 58", set()) == ["58"]


def test_a_number_present_in_evidence_is_not_unsupported():
    assert unsupported_numbers("the score is 58", {"58"}) == []


def test_the_motivating_example_flags_both_fabricated_figures():
    reply = (
        "the retrieved record shows score 58, rating moderate, source "
        "GlobalFloodModel v4, vintage 2021"
    )
    assert unsupported_numbers(reply, set()) == ["58", "2021"]


def test_unsupported_numbers_are_deduplicated_in_first_seen_order():
    assert unsupported_numbers("58 appears, then 58 again, then 99", set()) == ["58", "99"]


def test_unsupported_numbers_uses_original_surface_form():
    assert unsupported_numbers("1,234 units", set()) == ["1,234"]


def test_a_normalized_match_in_evidence_clears_a_differently_formatted_reply_token():
    # Evidence carries the normalized form ("1234"); the reply's own comma-
    # formatted surface form ("1,234") must still be recognised as supported.
    assert unsupported_numbers("1,234 units", {"1234"}) == []


def test_max_repairs_budget_is_three():
    # The model gets two rewrites; the third failing reply ships flagged
    # unresolved rather than being nudged again.
    assert GROUNDING_MAX_REPAIRS == 3


# ── unsupported_numbers: rounding, percent/fraction, arithmetic ────────────────
def test_a_rounded_reply_figure_clears_a_more_precise_evidence_value():
    # "58" is what a retrieved 58.37 rounds to — summarizing, not fabricating.
    assert unsupported_numbers("the score is 58", {"58.37"}) == []


def test_rounding_respects_the_replys_own_decimal_places():
    # The reply states one decimal place, so evidence is rounded to one place
    # too, not to a whole number — 58.4 does not clear evidence that itself
    # only rounds to 58.2 at that precision, but does clear evidence (58.37)
    # that rounds to 58.4 at one decimal place.
    assert unsupported_numbers("the score is 58.4", {"58.2"}) == ["58.4"]
    assert unsupported_numbers("the score is 58.4", {"58.2", "58.37"}) == []


def test_rounding_alone_does_not_manufacture_evidence_from_nothing():
    assert unsupported_numbers("the score is 58", set()) == ["58"]


def test_a_percent_token_clears_the_equivalent_evidence_fraction():
    assert unsupported_numbers("the rate is 22%", {"0.22"}) == []


def test_a_bare_fraction_token_clears_the_equivalent_evidence_percent_form():
    # The reverse direction: a bare (non-percent) reply number matches an
    # evidence value strictly between 0 and 1, scaled up by 100.
    assert unsupported_numbers("the rate is 22", {"0.22"}) == []


def test_the_fraction_rule_does_not_apply_outside_zero_to_one():
    # Evidence "22" itself must not be mistaken for a fraction of something
    # else — only evidence strictly between 0 and 1 qualifies.
    assert unsupported_numbers("the count is 2200", {"22"}) == ["2200"]


def test_arithmetic_sum_of_two_supported_reply_numbers_is_accepted():
    assert unsupported_numbers("the total of 30 and 50 is 80", {"30", "50"}) == []


def test_arithmetic_difference_of_two_supported_reply_numbers_is_accepted():
    assert unsupported_numbers("30 down from 50 is a drop of 20", {"30", "50"}) == []


def test_arithmetic_mean_of_two_supported_reply_numbers_is_accepted():
    assert unsupported_numbers("30 and 50 average to 40", {"30", "50"}) == []


def test_arithmetic_tolerates_half_a_unit_of_rounding():
    # 30.2 + 49.9 = 80.1 exactly; a reply that rounds the SUM to a bare "80"
    # (0 decimal places, so half a unit is 0.5) is within tolerance even
    # though no single evidence value rounds to 80 on its own (rule (b) alone
    # does not cover this — only arithmetic over the two evidenced addends
    # does).
    assert unsupported_numbers("30.2 and 49.9 sum to about 80", {"30.2", "49.9"}) == []


def test_arithmetic_does_not_fire_with_only_one_other_supported_number():
    # A single supported figure cannot "arithmetic" its way to an unrelated
    # one — 22 + 22, 22 - 22, and mean(22, 22) are all far from 80.
    assert unsupported_numbers("the score is 80, adjusted from 22", {"22"}) == ["80"]


def test_arithmetic_does_not_chain_off_another_arithmetic_result():
    # 30 and 50 support 80 by (d); 80 and 30 must NOT then be used to justify
    # some further figure by (d) again — arithmetic support does not chain.
    reply = "30 and 50 sum to 80; 80 and 30 sum to 110"
    assert unsupported_numbers(reply, {"30", "50"}) == ["110"]


def test_a_negative_case_with_no_evidence_at_all_still_fails():
    assert unsupported_numbers("the value is 58", set()) == ["58"]


def test_a_negative_case_with_an_unrelated_reply_number_still_fails():
    # "80" does not clear just because "22" also appears in the reply — 22 is
    # not itself evidenced, so it cannot support anything by arithmetic either.
    assert unsupported_numbers("scores of 80 and 22", set()) == ["80", "22"]


# ── run_has_retrieval_evidence ──────────────────────────────────────────────
def test_no_retrieval_and_no_tool_messages_has_no_evidence_source():
    assert run_has_retrieval_evidence([], []) is False
    assert run_has_retrieval_evidence([Msg(role="user", content="hi")], []) is False


def test_a_nonempty_retrieved_list_is_an_evidence_source():
    assert run_has_retrieval_evidence([], [{"dataset": "hazard_scores", "value": "58"}]) is True


def test_a_tool_result_message_is_an_evidence_source_even_on_this_runs_own_tool_call():
    # The run made a tool call (an assistant message's own `tool_calls`, and
    # the paired tool-role result answering it) — even though the lookup came
    # back empty, the check must still run rather than being treated as a
    # pure-knowledge answer with nothing to check against.
    messages = [
        Msg(
            role="assistant",
            content="looking it up",
            tool_calls=[ToolCall(id="tc1", name="lookup_dataset", arguments={"filters": {}})],
        ),
        Msg(role="tool", content="No rows in 'hazard_scores' match {}", tool_call_id="tc1"),
    ]
    assert run_has_retrieval_evidence(messages, []) is True
