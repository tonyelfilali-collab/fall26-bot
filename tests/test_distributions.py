"""
Tests for PLAN.md Step 8 (numeric and multiple choice), with worked examples.
"""
from __future__ import annotations

import asyncio

import numpy as np
import pytest
from forecasting_tools import (
    MultipleChoiceQuestion,
    NumericQuestion,
    Percentile,
    PredictedOption,
    PredictedOptionList,
)

import main
from distributions import (
    STEP8_PERCENTILES,
    median_multiple_choice,
    mix_with_uniform,
    pchip_cdf,
    pchip_distribution,
    pchip_eval,
)
from forecast_safety import distribution_problems


def question(**overrides) -> NumericQuestion:
    fields = dict(
        question_text="How many?", id_of_post=1, unit_of_measure="units",
        lower_bound=0.0, upper_bound=1000.0, open_lower_bound=False, open_upper_bound=False,
        zero_point=None, cdf_size=201,
    )
    fields.update(overrides)
    return NumericQuestion(**fields)


def nine(*values: float) -> list[Percentile]:
    return [Percentile(percentile=h, value=v) for h, v in zip(STEP8_PERCENTILES, values)]


def declared_at(distribution, value: float) -> float:
    """The CDF the distribution declares, at `value` (before the platform's final standardising)."""
    points = distribution.declared_percentiles
    return float(np.interp(value, [p.value for p in points], [p.percentile for p in points]))


def cdf_at(distribution, value: float) -> float:
    cdf = distribution.get_cdf()
    return float(np.interp(value, [p.value for p in cdf], [p.percentile for p in cdf]))


def options(*probabilities: float) -> PredictedOptionList:
    return PredictedOptionList(
        predicted_options=[PredictedOption(option_name=f"O{i}", probability=p) for i, p in enumerate(probabilities)]
    )


def _bot():
    return main.FallBot2026(
        llms={"default": "openrouter/x:free", "parser": "openrouter/x:free", "summarizer": "openrouter/x:free", "researcher": "no_research"},
        publish_reports_to_metaculus=False,
    )


# ---------------------------------------------------------------- PCHIP


def test_pchip_passes_through_every_point():
    x = np.array([0.0, 0.2, 0.5, 0.9, 1.0])
    y = np.array([0.0, 0.1, 0.5, 0.95, 1.0])
    assert pchip_eval(x, y, x) == pytest.approx(y)


def test_pchip_never_goes_down_or_overshoots():
    # Worked example: a flat stretch between 0.3 and 0.6 stays exactly flat.
    x = np.array([0.0, 0.3, 0.6, 1.0])
    y = np.array([0.0, 0.5, 0.5, 1.0])
    curve = pchip_eval(x, y, np.linspace(0, 1, 101))
    assert np.all(np.diff(curve) >= -1e-12)
    assert curve.min() >= 0 and curve.max() <= 1
    assert pchip_eval(x, y, np.array([0.45]))[0] == pytest.approx(0.5)


def test_worked_example_linear_question():
    # 0-1000, closed bounds; the model's median is 300.
    q = question()
    d = pchip_distribution(nine(50, 80, 120, 200, 300, 420, 560, 650, 720), q)
    assert distribution_problems(d, q) == []
    assert cdf_at(d, 300) == pytest.approx(0.50, abs=0.01)
    assert cdf_at(d, 120) == pytest.approx(0.10, abs=0.01)
    assert cdf_at(d, 650) == pytest.approx(0.95, abs=0.01)


def test_closed_bounds_pin_the_ends():
    cdf = pchip_cdf(nine(50, 80, 120, 200, 300, 420, 560, 650, 720), question())
    assert cdf[0] == pytest.approx(0.0) and cdf[-1] == pytest.approx(1.0)


def test_open_bounds_keep_tails():
    q = question(open_lower_bound=True, open_upper_bound=True)
    d = pchip_distribution(nine(50, 80, 120, 200, 300, 420, 560, 650, 720), q)
    cdf = [p.percentile for p in d.get_cdf()]
    assert cdf[0] >= 0.001 and cdf[-1] <= 0.999
    assert distribution_problems(d, q) == []


def test_log_scaled_question_median_in_the_right_place():
    q = question(lower_bound=1.0, upper_bound=1e6, zero_point=0.0, open_lower_bound=True, open_upper_bound=True)
    d = pchip_distribution(nine(20, 40, 90, 300, 1000, 4000, 15000, 30000, 60000), q)
    assert distribution_problems(d, q) == []
    assert cdf_at(d, 1000) == pytest.approx(0.5, abs=0.02)


def test_discrete_question():
    q = question(upper_bound=10.0, cdf_size=11)
    d = pchip_distribution(nine(0.5, 1, 1.5, 2.5, 4, 5.5, 7, 8, 9), q)
    assert len(d.get_cdf()) == 11
    assert distribution_problems(d, q) == []


# ---------------------------------------------------------------- combining numeric forecasts


def test_worked_example_pointwise_median_of_three_models():
    # final(x) = 0.95 x median of the three models' CDFs at x + 0.05 x location(x)
    q = question()
    low = pchip_distribution(nine(20, 40, 60, 100, 150, 220, 300, 350, 400), q)
    mid = pchip_distribution(nine(50, 80, 120, 200, 300, 420, 560, 650, 720), q)
    high = pchip_distribution(nine(300, 350, 400, 480, 600, 700, 800, 850, 900), q)
    combined = asyncio.run(_bot()._aggregate_predictions([low, mid, high], q))
    for value in (100, 300, 500, 700):
        middle = sorted([declared_at(low, value), declared_at(mid, value), declared_at(high, value)])[1]
        assert declared_at(combined, value) == pytest.approx(0.95 * middle + 0.05 * value / 1000, abs=1e-6)


def test_worked_example_uniform_mix():
    # A model 90% sure the answer is below 500: 0.95 x 0.90 + 0.05 x 0.5 = 0.88.
    q = question()
    d = pchip_distribution(nine(100, 150, 200, 300, 350, 420, 500, 600, 700), q)
    model_at_500 = declared_at(d, 500)
    assert model_at_500 == pytest.approx(0.90, abs=0.01)
    mixed = mix_with_uniform(d, q)
    assert declared_at(mixed, 500) == pytest.approx(0.95 * model_at_500 + 0.05 * 0.5, abs=1e-6)
    assert declared_at(mixed, 500) == pytest.approx(0.88, abs=0.01)
    assert distribution_problems(mixed, q) == []


def test_bot_numeric_final_forecast_is_mixed_and_valid():
    q = question(open_upper_bound=True)
    forecast = pchip_distribution(nine(50, 80, 120, 200, 300, 420, 560, 650, 720), q)
    final = asyncio.run(_bot()._aggregate_predictions([forecast, forecast], q))
    assert distribution_problems(final, q) == []
    assert declared_at(final, 300) == pytest.approx(0.95 * declared_at(forecast, 300) + 0.05 * 0.3, abs=1e-6)


# ---------------------------------------------------------------- multiple choice


def test_worked_example_multiple_choice_median_per_option():
    # Medians 0.2, 0.4, 0.3 (sum 0.9) -> renormalised 2/9, 4/9, 3/9.
    result = median_multiple_choice([options(0.2, 0.3, 0.5), options(0.4, 0.4, 0.2), options(0.1, 0.6, 0.3)])
    assert [o.probability for o in result.predicted_options] == pytest.approx([2 / 9, 4 / 9, 3 / 9])


def test_multiple_choice_median_then_floor():
    # Medians 0.0, 0.2, 0.8 -> option 0 lifted to 1%; the other two share the
    # remaining 99% in their 1:4 ratio: 0.198 and 0.792.
    result = median_multiple_choice([options(0.0, 0.2, 0.8), options(0.0, 0.1, 0.9), options(0.1, 0.2, 0.7)])
    probabilities = [o.probability for o in result.predicted_options]
    assert probabilities == pytest.approx([0.01, 0.198, 0.792])


def test_median_differs_from_mean_with_an_outlier():
    # One model puts 90% on O0; the median ignores the outlier, the mean doesn't.
    predictions = [options(0.9, 0.05, 0.05), options(0.2, 0.4, 0.4), options(0.2, 0.4, 0.4)]
    result = median_multiple_choice(predictions)
    assert result.predicted_options[0].probability == pytest.approx(0.2)


def test_bot_multiple_choice_uses_the_median():
    q = MultipleChoiceQuestion(question_text="Which?", id_of_post=1, options=["O0", "O1", "O2"])
    predictions = [options(0.9, 0.05, 0.05), options(0.2, 0.4, 0.4), options(0.2, 0.4, 0.4)]
    bot = main.FallBot2026(
        llms={"default": "openrouter/x:free", "parser": "openrouter/x:free", "summarizer": "openrouter/x:free", "researcher": "no_research"},
        publish_reports_to_metaculus=False,
    )
    result = asyncio.run(bot._aggregate_predictions(predictions, q))
    assert result.predicted_options[0].probability == pytest.approx(0.2)
