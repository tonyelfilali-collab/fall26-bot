"""
Tests for the safety checks on forecasts (PLAN.md Step 4), including the
known disasters: 99% on an unresolved question, a x1000 unit error, and
reversed percentiles. No network, no real model calls.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from forecasting_tools import (
    BinaryQuestion,
    MultipleChoiceQuestion,
    NumericDistribution,
    NumericQuestion,
    Percentile,
    PredictedOption,
    PredictedOptionList,
    ReasonedPrediction,
)

import forecast_safety as fs
import main

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def options(*probabilities: float) -> PredictedOptionList:
    return PredictedOptionList(
        predicted_options=[PredictedOption(option_name=f"O{i}", probability=p) for i, p in enumerate(probabilities)]
    )


def numeric_question(**overrides) -> NumericQuestion:
    fields = dict(
        question_text="How many widgets will be sold in 2026?", id_of_post=11,
        page_url="https://www.metaculus.com/questions/11", unit_of_measure="thousand widgets",
        lower_bound=0.0, upper_bound=1000.0, open_lower_bound=False, open_upper_bound=True,
        zero_point=None, cdf_size=201,
    )
    fields.update(overrides)
    return NumericQuestion(**fields)


def percentiles(*values: float) -> list[Percentile]:
    heights = [0.1, 0.2, 0.4, 0.6, 0.8, 0.9]
    return [Percentile(percentile=h, value=v) for h, v in zip(heights, values)]


# ---------------------------------------------------------------- binary: the 5 steps


@pytest.mark.parametrize(
    "given, expected",
    [(0.0, 0.02), (0.01, 0.02), (0.02, 0.02), (0.5, 0.5), (0.98, 0.98), (0.99, 0.98), (1.0, 0.98)],
)
def test_clip_binary_2_to_98(given, expected):
    assert fs.clip_binary(given) == pytest.approx(expected)


def test_stretch_is_off_by_default():
    assert fs.STRETCH_K == 1.0
    for p in (0.03, 0.3, 0.5, 0.8, 0.97):
        assert fs.stretch(p) == pytest.approx(p)


def test_stretch_when_turned_on_pushes_away_from_half():
    assert fs.stretch(0.7, k=1.2) > 0.7
    assert fs.stretch(0.3, k=1.2) < 0.3
    assert fs.stretch(0.5, k=1.2) == pytest.approx(0.5)


def test_median_is_step_one():
    assert fs.adjust_binary([0.2, 0.4, 0.9]) == pytest.approx(0.4)


def test_disaster_99_percent_on_an_unresolved_question_is_pulled_back():
    # One of three forecasts disagrees: extreme not allowed, back to 90%.
    assert fs.adjust_binary([0.99, 0.99, 0.6]) == pytest.approx(0.90)


def test_extreme_allowed_when_forecasts_agree_then_clipped():
    assert fs.adjust_binary([0.99, 0.99, 0.97]) == pytest.approx(0.98)
    assert fs.adjust_binary([0.01, 0.02, 0.03]) == pytest.approx(0.02)


def test_extreme_low_pulled_back_without_agreement():
    assert fs.adjust_binary([0.01, 0.02, 0.3]) == pytest.approx(0.10)


def test_extreme_check_uses_80_percent_of_forecasts_that_ran():
    assert fs.extreme_check(0.97, [0.95, 0.96, 0.97, 0.98, 0.5]) == pytest.approx(0.97)  # 4/5 = 80%
    assert fs.extreme_check(0.97, [0.95, 0.96, 0.97, 0.5, 0.5]) == pytest.approx(0.90)  # 3/5


def test_non_extreme_forecasts_pass_through():
    assert fs.adjust_binary([0.07, 0.3, 0.6]) == pytest.approx(0.3)


def test_invalid_binary_forecasts_are_ignored_or_fall_back():
    assert fs.adjust_binary([float("nan"), 1.7, 0.3]) == pytest.approx(0.3)
    assert fs.adjust_binary([]) == fs.BINARY_FALLBACK
    assert fs.adjust_binary([float("nan")]) == fs.BINARY_FALLBACK


# ---------------------------------------------------------------- multiple choice


def test_multiple_choice_floor_lifts_small_options_and_sums_to_one():
    result = [o.probability for o in fs.floor_multiple_choice(options(0.0, 0.005, 0.995)).predicted_options]
    assert min(result) >= 0.01 - 1e-12
    assert sum(result) == pytest.approx(1.0)
    assert result[0] <= result[1] <= result[2]


def test_multiple_choice_unchanged_when_already_fine():
    given = options(0.2, 0.3, 0.5)
    assert fs.floor_multiple_choice(given) == given


def test_uniform_multiple_choice_fallback():
    result = fs.uniform_multiple_choice(["a", "b", "c", "d"])
    assert [o.probability for o in result.predicted_options] == pytest.approx([0.25] * 4)


# ---------------------------------------------------------------- numeric / discrete


def _valid_cdf(size=201, start=0.0, end=1.0):
    return [start + (end - start) * i / (size - 1) for i in range(size)]


def test_valid_cdf_passes():
    assert fs.cdf_problems(_valid_cdf(), 201, False, False) == []


def test_cdf_wrong_size():
    assert fs.cdf_problems(_valid_cdf(200), 201, False, False)


def test_cdf_must_increase():
    cdf = _valid_cdf()
    cdf[100] = cdf[99]
    assert any("increase" in p for p in fs.cdf_problems(cdf, 201, False, False))


def test_cdf_step_at_most_0_2():
    cdf = [0.0] + [0.3 + 0.7 * i / 199 for i in range(200)]
    assert any("at most" in p for p in fs.cdf_problems(cdf, 201, False, False))


def test_cdf_bounds_closed_and_open():
    assert any("start at 0" in p for p in fs.cdf_problems(_valid_cdf(start=0.01), 201, False, False))
    assert any("end at 1" in p for p in fs.cdf_problems(_valid_cdf(end=0.99), 201, False, False))
    assert any("0.001 or more" in p for p in fs.cdf_problems(_valid_cdf(start=0.0), 201, True, True))
    assert any("0.999 or less" in p for p in fs.cdf_problems(_valid_cdf(start=0.001, end=1.0), 201, True, True))
    assert fs.cdf_problems(_valid_cdf(start=0.001, end=0.999), 201, True, True) == []


def test_library_distribution_passes_the_platform_rules():
    question = numeric_question()
    distribution = NumericDistribution.from_question(percentiles(100, 200, 350, 500, 700, 850), question)
    assert fs.distribution_problems(distribution, question) == []


def test_discrete_question_distribution_passes():
    question = numeric_question(cdf_size=11, upper_bound=10.0, open_upper_bound=False, question_text="How many?")
    distribution = NumericDistribution.from_question(percentiles(1, 2, 4, 5, 7, 9), question)
    assert fs.distribution_problems(distribution, question) == []


def test_disaster_reversed_percentiles_are_put_right():
    fixed = fs.fix_reversed_percentiles(percentiles(850, 700, 500, 350, 200, 100))
    assert [p.value for p in fixed] == [100, 200, 350, 500, 700, 850]
    assert [p.percentile for p in fixed] == [0.1, 0.2, 0.4, 0.6, 0.8, 0.9]


def test_increasing_percentiles_are_left_alone():
    given = percentiles(100, 200, 350, 500, 700, 850)
    assert [p.value for p in fs.fix_reversed_percentiles(given)] == [100, 200, 350, 500, 700, 850]


def test_disaster_x1000_unit_error_is_detected():
    assert fs.all_outside_range(percentiles(100e3, 200e3, 350e3, 500e3, 700e3, 850e3), 0, 1000)
    assert not fs.all_outside_range(percentiles(100, 200, 350, 500, 700, 1200), 0, 1000)


def test_wide_fallback_distribution_is_valid():
    question = numeric_question()
    fallback = fs.wide_fallback_distribution(question)
    assert fs.distribution_problems(fallback, question) == []
    values = [p.value for p in fallback.declared_percentiles]
    assert values[0] > 0 and values[-1] < 1000


def test_wide_fallback_distribution_log_scaled():
    question = numeric_question(lower_bound=1.0, upper_bound=1e6, zero_point=0.0, open_lower_bound=True)
    fallback = fs.wide_fallback_distribution(question)
    assert fs.distribution_problems(fallback, question) == []


# ---------------------------------------------------------------- before submitting


def test_open_question_can_be_submitted():
    q = SimpleNamespace(state=SimpleNamespace(value="open"), close_time=NOW + timedelta(hours=1), actual_resolution_time=None, resolution_string=None)
    assert fs.still_open_problem(q, NOW) is None


@pytest.mark.parametrize(
    "fields, reason",
    [
        (dict(state=SimpleNamespace(value="closed")), "closed"),
        (dict(state=SimpleNamespace(value="resolved")), "resolved"),
        (dict(actual_resolution_time=NOW - timedelta(hours=1)), "resolved"),
        (dict(resolution_string="yes"), "resolved"),
        (dict(close_time=NOW - timedelta(minutes=1)), "closed"),
    ],
)
def test_closed_or_resolved_question_is_not_submitted(fields, reason):
    q = dict(state=SimpleNamespace(value="open"), close_time=NOW + timedelta(hours=1), actual_resolution_time=None, resolution_string=None)
    q.update(fields)
    assert reason in fs.still_open_problem(SimpleNamespace(**q), NOW)


def test_dates_line_has_today_close_and_resolve():
    q = SimpleNamespace(close_time=datetime(2026, 10, 1, tzinfo=timezone.utc), scheduled_resolution_time=datetime(2026, 12, 31, tzinfo=timezone.utc))
    line = fs.dates_line(q, NOW)
    assert "2026-09-28" in line and "2026-10-01" in line and "2026-12-31" in line


# ---------------------------------------------------------------- in the bot


def _bot():
    return main.FallBot2026(
        llms={"default": "openrouter/x:free", "parser": "openrouter/x:free", "summarizer": "openrouter/x:free", "researcher": "no_research"},
        publish_reports_to_metaculus=False,
    )


def test_bot_final_binary_goes_through_the_5_steps():
    question = BinaryQuestion(question_text="Will X?", id_of_post=1)
    assert asyncio.run(_bot()._aggregate_predictions([0.99, 0.99, 0.6], question)) == pytest.approx(0.90)
    assert asyncio.run(_bot()._aggregate_predictions([0.99, 0.99, 0.98], question)) == pytest.approx(0.98)


def test_bot_floors_final_multiple_choice_forecast():
    question = MultipleChoiceQuestion(question_text="Which?", id_of_post=1, options=["O0", "O1", "O2"])
    result = asyncio.run(_bot()._aggregate_predictions([options(0.0, 0.1, 0.9), options(0.0, 0.2, 0.8)], question))
    probabilities = [o.probability for o in result.predicted_options]
    assert min(probabilities) >= 0.01 - 1e-12
    assert sum(probabilities) == pytest.approx(1.0)


def test_every_prompt_includes_the_dates(monkeypatch):
    seen = []

    async def capture(self, question, prompt):
        seen.append(prompt)
        return ReasonedPrediction(prediction_value=0.5, reasoning="x")

    monkeypatch.setattr(main.FallBot2026, "_binary_prompt_to_forecast", capture)
    question = BinaryQuestion(
        question_text="Will X?", id_of_post=1,
        close_time=datetime(2026, 10, 1, tzinfo=timezone.utc),
        scheduled_resolution_time=datetime(2026, 12, 31, tzinfo=timezone.utc),
    )
    asyncio.run(_bot()._run_forecast_on_binary(question, "research"))
    assert "closes on 2026-10-01" in seen[0] and "resolve on 2026-12-31" in seen[0]
    assert "Today is " in seen[0]


def _parse_bot(monkeypatch, parses):
    answers = iter(parses)

    async def fake_structure_output(text, output_type, model=None, additional_instructions=None, num_validation_samples=1):
        return next(answers)

    monkeypatch.setattr(main, "structure_output", fake_structure_output)
    bot = _bot()
    monkeypatch.setattr(main.FallBot2026, "get_llm", lambda self, purpose="default", guarantee_type=None: None)
    return bot


def test_disaster_x1000_unit_error_is_reparsed_once(monkeypatch):
    question = numeric_question()
    bot = _parse_bot(monkeypatch, [percentiles(100e3, 200e3, 350e3, 500e3, 700e3, 850e3), percentiles(100, 200, 350, 500, 700, 850)])
    result = asyncio.run(bot._parse_numeric_safely(question, "reasoning", "instructions"))
    assert [p.value for p in result.declared_percentiles] == [100, 200, 350, 500, 700, 850]


def test_disaster_x1000_unit_error_twice_uses_the_wide_fallback(monkeypatch):
    question = numeric_question()
    wrong = percentiles(100e3, 200e3, 350e3, 500e3, 700e3, 850e3)
    bot = _parse_bot(monkeypatch, [wrong, wrong])
    result = asyncio.run(bot._parse_numeric_safely(question, "reasoning", "instructions"))
    assert fs.distribution_problems(result, question) == []
    assert all(0 < p.value < 1000 for p in result.declared_percentiles)


def test_disaster_reversed_percentiles_in_the_bot(monkeypatch):
    question = numeric_question()
    bot = _parse_bot(monkeypatch, [percentiles(850, 700, 500, 350, 200, 100)])
    result = asyncio.run(bot._parse_numeric_safely(question, "reasoning", "instructions"))
    assert [p.value for p in result.declared_percentiles] == [100, 200, 350, 500, 700, 850]


@pytest.mark.parametrize(
    "question",
    [
        BinaryQuestion(question_text="Will X?", id_of_post=1),
        MultipleChoiceQuestion(question_text="Which?", id_of_post=1, options=["a", "b", "c"]),
        numeric_question(),
    ],
)
def test_fallback_report_for_every_question_type(question):
    report = _bot()._fallback_report(question, RuntimeError("everything failed"))
    assert report.prediction is not None
    assert report.explanation.startswith("#")


def test_question_that_fails_completely_still_gets_a_fallback(monkeypatch):
    async def always_fails(self, question):
        raise RuntimeError("all models down")

    monkeypatch.setattr(main.ForecastBot, "_run_individual_question", always_fails)
    bot = _bot()
    question = BinaryQuestion(question_text="Will X?", id_of_post=3)
    [report] = asyncio.run(bot.forecast_questions([question], return_exceptions=True))
    assert not isinstance(report, BaseException)
    assert report.prediction == pytest.approx(0.5)
    assert bot.fallback_count == 1
