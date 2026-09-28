"""
The unit check (PLAN.md Step 4 leftover): the dossier's current value, the
"median more than 10x / less than 0.1x the current value" check, the re-parse,
and the wide distribution around the current value. Worked example: a x1000
unit error (thousands written as units).
"""
from __future__ import annotations

import asyncio
import json
import math
from types import SimpleNamespace

import numpy as np
import pytest
from forecasting_tools import NumericQuestion, Percentile

import forecast_safety as fs
import main
import research
from distributions import STEP8_PERCENTILES


def nine(*values):
    return [Percentile(percentile=h, value=v) for h, v in zip(STEP8_PERCENTILES, values)]


RIGHT = nine(150, 170, 190, 220, 250, 280, 320, 350, 380)  # thousand widgets
X1000 = nine(*(p.value * 1000 for p in RIGHT))  # the same, in widgets: a x1000 error (outside the range)
X11 = nine(*(p.value * 11 for p in RIGHT))  # inside the range, but the median is 11x the current value


def question():
    return NumericQuestion(
        question_text="How many thousand widgets will be sold in 2026?", id_of_post=21,
        page_url="https://www.metaculus.com/questions/21", unit_of_measure="thousand widgets",
        lower_bound=0.0, upper_bound=5_000.0, open_lower_bound=False, open_upper_bound=True,
        zero_point=None, cdf_size=201,
    )


# ---------------------------------------------------------------- the current value in the dossier


def test_parse_current_value():
    cv = research.parse_current_value("## Current status\nCURRENT VALUE: 1,234.5 | UNIT: thousand widgets | DATE: 2026-09-20")
    assert (cv.value, cv.unit, cv.date) == (1234.5, "thousand widgets", "2026-09-20")
    assert research.parse_current_value("CURRENT VALUE: unknown") is None


def test_dossier_asks_for_the_current_value_only_on_numeric_questions():
    prompts = []

    async def helper(prompt):
        prompts.append(prompt)
        if "plan news research" in prompt:
            return json.dumps({"queries": ["widgets"], "key_facts": []})
        return "## Current status\nCURRENT VALUE: 250 | UNIT: thousand widgets | DATE: 2026-09-20\nMISSING: none"

    async def asknews(query):
        return []

    numeric = SimpleNamespace(question_text="How many?", resolution_criteria="", id_of_post=1, question_type="numeric", unit_of_measure="thousand widgets")
    result = asyncio.run(research.run_planned_research(numeric, "Today is x.", helper, asknews, lambda q: []))
    assert "CURRENT VALUE:" in prompts[1] and "thousand widgets" in prompts[1]
    assert result.current_value.value == 250
    binary = SimpleNamespace(question_text="Will X?", resolution_criteria="", id_of_post=2, question_type="binary")
    prompts.clear()
    asyncio.run(research.run_planned_research(binary, "Today is x.", helper, asknews, lambda q: []))
    assert "CURRENT VALUE:" not in prompts[1]


# ---------------------------------------------------------------- the check


def test_worked_example_x1000_is_caught():
    # Median 250,000 vs current value 250: ratio 1,000 > 10.
    assert fs.median_of(X1000) == 250_000
    assert fs.off_by_10x(X1000, 250) is True
    assert fs.off_by_10x(RIGHT, 250) is False


@pytest.mark.parametrize(
    "median, current, flagged",
    [(2600, 250, True), (2400, 250, False), (26, 250, False), (24, 250, True), (250, None, False), (250, 0, False), (-5, 250, False)],
)
def test_10x_boundaries(median, current, flagged):
    assert fs.off_by_10x(nine(*(median * f for f in (0.5, 0.6, 0.7, 0.9, 1, 1.1, 1.3, 1.4, 1.5))), current) is flagged


# ---------------------------------------------------------------- in the bot


def _bot(monkeypatch, parses, current_value=250):
    answers = iter(parses)

    async def fake_structure_output(text, output_type, model=None, additional_instructions=None, num_validation_samples=1):
        return next(answers)

    monkeypatch.setattr(main, "structure_output", fake_structure_output)
    monkeypatch.setattr(main.FallBot2026, "get_llm", lambda self, purpose="default", guarantee_type=None: None)
    bot = main.FallBot2026(
        llms={"default": "openrouter/x:free", "parser": "openrouter/x:free", "summarizer": "openrouter/x:free", "researcher": "no_research"},
        publish_reports_to_metaculus=False,
    )
    q = question()
    bot._record_for(q)["research_detail"] = {"current_value": {"value": current_value, "unit": "thousand widgets", "date": "2026-09-20"} if current_value else None}
    return bot, q


def cdf_at(distribution, value):
    points = distribution.declared_percentiles
    return float(np.interp(value, [p.value for p in points], [p.percentile for p in points]))


def test_x1000_error_is_reparsed_once(monkeypatch):
    bot, q = _bot(monkeypatch, [X1000, RIGHT])
    result = asyncio.run(bot._parse_numeric_safely(q, "no percentile lines here", "instructions"))
    assert cdf_at(result, 250) == pytest.approx(0.5, abs=0.02)


def test_10x_inside_the_range_is_reparsed(monkeypatch):
    bot, q = _bot(monkeypatch, [X11, RIGHT])
    result = asyncio.run(bot._parse_numeric_safely(q, "no percentile lines here", "instructions"))
    assert cdf_at(result, 250) == pytest.approx(0.5, abs=0.02)


def test_without_a_current_value_the_10x_check_is_skipped(monkeypatch):
    bot, q = _bot(monkeypatch, [X11], current_value=None)
    result = asyncio.run(bot._parse_numeric_safely(q, "no percentile lines here", "instructions"))
    assert cdf_at(result, 2750) == pytest.approx(0.5, abs=0.02)  # accepted as parsed


def test_outside_the_range_twice_without_a_current_value_is_dropped(monkeypatch):
    bot, q = _bot(monkeypatch, [X1000, X1000], current_value=None)
    with pytest.raises(fs.NoValidForecast):
        asyncio.run(bot._parse_numeric_safely(q, "no percentile lines here", "instructions"))


# ---------------------------------------------------------------- still flagged after the re-ask


def _flagged_forecast(monkeypatch, bot, q, parses):
    answers = iter(parses)

    async def fake_structure_output(text, output_type, model=None, additional_instructions=None, num_validation_samples=1):
        return next(answers)

    monkeypatch.setattr(main, "structure_output", fake_structure_output)
    return asyncio.run(bot._parse_numeric_safely(q, "no percentile lines here", "instructions"))


def test_one_model_still_10x_off_is_dropped(monkeypatch):
    # Worked example: two models say ~250 thousand, one keeps saying ~2,750 (x11)
    # after the re-ask: that one is dropped; the final median is ~250.
    bot, q = _bot(monkeypatch, [RIGHT])
    good_1 = _flagged_forecast(monkeypatch, bot, q, [RIGHT])
    good_2 = _flagged_forecast(monkeypatch, bot, q, [RIGHT])
    bad = _flagged_forecast(monkeypatch, bot, q, [X11, X11])
    final = asyncio.run(bot._aggregate_predictions([good_1, bad, good_2], q))
    assert cdf_at(final, 250) == pytest.approx(0.95 * 0.5 + 0.05 * 250 / 5000, abs=0.02)


def test_every_model_10x_off_keeps_them_all(monkeypatch, caplog):
    # All three say ~2,750 while research says 250: research is probably wrong.
    bot, q = _bot(monkeypatch, [X11])
    forecasts = [_flagged_forecast(monkeypatch, bot, q, [X11, X11]) for _ in range(3)]
    with caplog.at_level("WARNING", logger="fall26"):
        final = asyncio.run(bot._aggregate_predictions(forecasts, q))
    assert cdf_at(final, 2750) == pytest.approx(0.95 * 0.5 + 0.05 * 2750 / 5000, abs=0.02)
    assert "keeping them all" in caplog.text


def test_no_made_up_forecast_anywhere():
    import forecast_safety

    assert not hasattr(forecast_safety, "wide_around")
