"""
Current-value check (research.py, architect 28 Oct): when the question states
a dated current value and the dossier's CURRENT VALUE differs by more than
20%, one "Note:" line is added and the unit check uses the more recent of the
two (neither unless both are dated). Fake helper, no network.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import research
from research import CurrentValue, StatedValue, check_current_value, stated_value

TODAY = date(2026, 10, 20)
WHEAT_BACKGROUND = (
    "The December 2026 CBOT wheat futures contract (ZWZ26) is quoted in USD per bushel. "
    "Dec 2026 wheat futures (ZWZ26) settled at about 6.83 on October 2, 2026."
)


def wheat_question(background=WHEAT_BACKGROUND):
    return SimpleNamespace(
        question_text="What will the settlement price of CBOT December 2026 wheat futures be on October 15, 2026?",
        resolution_criteria="Resolves to the ZWZ26 settlement price on October 15, 2026, per CME.",
        background_info=background,
        fine_print="",
        question_type="numeric",
        unit_of_measure="USD per bushel",
        open_time=datetime(2026, 10, 3, tzinfo=timezone.utc),
        id_of_post=45899,
    )


class Helper:
    def __init__(self, dossier: str):
        self.dossier = dossier

    async def __call__(self, prompt: str) -> str:
        if "plan news research" in prompt:
            return json.dumps({"queries": ["wheat futures"], "key_facts": ["latest settlement"]})
        return self.dossier


def run(question, value_line: str):
    dossier = f"## Current status\nWheat is weak.\n{value_line}\nMISSING: none"

    async def no_news(query):
        return []

    return asyncio.run(research.run_planned_research(question, "Today is 2026-10-03.", Helper(dossier), no_news,
                                                     lambda query: []))


NOTE = ("Note: the question says 6.83 (2026-10-02); our research says 5.5675 (2025-12-15). "
        "Check which is more recent.")


def test_wheat_style_note_and_the_unit_check_uses_the_question_value():
    result = run(wheat_question(), "CURRENT VALUE: 5.5675 | UNIT: USD per bushel | DATE: 2025-12-15")
    assert result.dossier.endswith(NOTE) and result.dossier.count("Note: the question says") == 1
    assert result.current_value == CurrentValue(6.83, "USD per bushel", "2026-10-02")
    assert result.value_check["used"] == "question"
    assert result.value_check["research"] == {"value": 5.5675, "date": "2025-12-15"}


def test_within_20_percent_no_line_and_unchanged():
    result = run(wheat_question(), "CURRENT VALUE: 6.20 | UNIT: USD per bushel | DATE: 2026-10-01")
    assert "Note: the question says" not in result.dossier
    assert result.current_value == CurrentValue(6.2, "USD per bushel", "2026-10-01") and result.value_check is None


def test_question_value_without_a_date_no_line():
    background = "Dec 2026 wheat futures (ZWZ26) currently trade at about 6.83 per bushel."
    result = run(wheat_question(background), "CURRENT VALUE: 5.5675 | UNIT: USD per bushel | DATE: 2025-12-15")
    assert "Note: the question says" not in result.dossier
    assert result.current_value.value == 5.5675 and result.value_check is None


def test_both_dated_research_newer_uses_research():
    stated = StatedValue(6.83, "6.83", date(2026, 10, 2))
    ours = CurrentValue(5.10, "USD per bushel", "2026-10-14")
    chosen, note, record = check_current_value(ours, stated)
    assert chosen is ours and record["used"] == "research"
    assert note == ("Note: the question says 6.83 (2026-10-02); our research says 5.1 (2026-10-14). "
                    "Check which is more recent.")


def test_research_undated_line_appears_but_neither_is_used():
    result = run(wheat_question(), "CURRENT VALUE: 5.5675 | UNIT: USD per bushel | DATE: unknown")
    assert result.dossier.endswith("our research says 5.5675 (no date). Check which is more recent.")
    assert result.current_value is None and result.value_check["used"] == "neither"


def test_same_day_disagreement_uses_neither():
    chosen, note, record = check_current_value(CurrentValue(10.0, "$/bu", "2026-10-02"),
                                               StatedValue(6.83, "6.83", date(2026, 10, 2)))
    assert chosen is None and note and record["used"] == "neither"


def test_no_research_value_nothing_happens():
    assert check_current_value(None, StatedValue(6.83, "6.83", date(2026, 10, 2))) == (None, None, None)


def test_stated_value_formats():
    year = 2026
    cases = {
        "Brent closed at $98.40 on Oct 14.": (98.4, date(2026, 10, 14)),
        "The index was 11,722.46 as of 2 October 2026.": (11722.46, date(2026, 10, 2)),
        "The latest price is $1,234.5 (Oct 2, 2026).": (1234.5, date(2026, 10, 2)),
        "As of September 30, 2026, the unemployment rate stood at 4.3%.": (4.3, date(2026, 9, 30)),
        "The rate was 4.1% on 2026-09-01. It was 4.3% on 2026-10-01.": (4.3, date(2026, 10, 1)),  # the latest one
    }
    for text, (value, day) in cases.items():
        found = stated_value([text], year, today=TODAY)
        assert found is not None and (found.value, found.day) == (value, day), text


def test_stated_value_is_conservative():
    for text in (
        "The question resolves on October 15, 2026.",  # no value word + number
        "In 2025 the price was 300.",  # a value but no date beside it
        "The event happened in 2024 on October 2, 2026.",  # a bare year is never a value
        "Up to 5 questions open on Oct 2.",  # no value word
        "Will the price be above $100 on October 15, 2026?",  # a threshold, not a current value
        "Brent closed at $98.40 on Oct 30.",  # a date after today
    ):
        assert stated_value([text], 2026, today=TODAY) is None, text


def test_real_wheat_wording_cents_and_dollars():
    # 45899's own background: the cents figure, with the dollar figure in brackets.
    text = "As of October 2, 2026, the ZWZ26 contract settled at about 683 cents ($6.83) per bushel."
    stated = stated_value([text], 2026, today=TODAY)
    assert (stated.value, stated.alternatives, stated.day) == (683.0, ("6.83",), date(2026, 10, 2))
    chosen, note, record = check_current_value(CurrentValue(5.5675, "$/bu", "2025-12-15"), stated)
    assert note == NOTE and chosen.value == 6.83 and record["used"] == "question"
    # Within 20% in the research's unit: no line.
    assert check_current_value(CurrentValue(6.5, "$/bu", "2026-10-04"), stated)[1] is None


def test_disagree_rule():
    assert not research.disagree(6.0, 7.2) and research.disagree(6.0, 7.21) and research.disagree(7.21, 6.0)
    assert research.disagree(-21.0, 38.2) and research.disagree(0.0, 1.0) and not research.disagree(0.0, 0.0)


def test_45936_style_percent_vs_net_approval_is_skipped():
    # The background gives the approve share; the question (and our research) is the net approval.
    text = ("As of October 1, 2026, the RCP Average (covering the 9/8-10/1 window) stood at 38.2% approve "
            "and 60.3% disapprove.")
    stated = stated_value([text], 2026, today=TODAY)
    assert (stated.value, stated.percent) == (38.2, True)
    research_value = CurrentValue(-21.0, "points (approve minus disapprove)", "2026-10-05")
    assert check_current_value(research_value, stated) == (research_value, None, None)


def test_exactly_one_percentage_is_skipped():
    stated = stated_value(["The unemployment rate was 4.3 percent on Oct 1."], 2026, today=TODAY)
    assert stated.percent is True
    ours = CurrentValue(7.0, "people (millions)", "2026-09-01")
    assert check_current_value(ours, stated) == (ours, None, None)
    # Both percentages: still checked.
    chosen, note, record = check_current_value(CurrentValue(7.0, "%", "2026-09-01"), stated)
    assert note and record["used"] == "question" and chosen.value == 4.3


def test_signs_differ_is_skipped():
    stated = StatedValue(12.5, "12.5", date(2026, 10, 2))
    ours = CurrentValue(-3.0, "index points", "2026-09-20")
    assert check_current_value(ours, stated) == (ours, None, None)


def test_dates_without_a_year_use_the_question_year():
    found = stated_value(["Settled at 6.83 on Oct 2."], 2027, today=date(2027, 10, 5))
    assert found.day == date(2027, 10, 2)
    assert research.question_year(wheat_question()) == 2026
