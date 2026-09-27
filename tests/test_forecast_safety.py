"""Tests for the interim safety limits on final forecasts."""
from __future__ import annotations

import asyncio

import pytest
from forecasting_tools import (
    BinaryQuestion,
    MultipleChoiceQuestion,
    PredictedOption,
    PredictedOptionList,
)

import main
from forecast_safety import clip_binary, floor_multiple_choice


def options(*probabilities: float) -> PredictedOptionList:
    return PredictedOptionList(
        predicted_options=[
            PredictedOption(option_name=f"O{i}", probability=p)
            for i, p in enumerate(probabilities)
        ]
    )


@pytest.mark.parametrize(
    "given, expected",
    [(0.0, 0.03), (0.01, 0.03), (0.03, 0.03), (0.5, 0.5), (0.97, 0.97), (0.99, 0.97), (1.0, 0.97)],
)
def test_clip_binary(given, expected):
    assert clip_binary(given) == pytest.approx(expected)


def test_multiple_choice_floor_lifts_small_options_and_sums_to_one():
    result = [o.probability for o in floor_multiple_choice(options(0.0, 0.005, 0.995)).predicted_options]
    assert min(result) >= 0.01 - 1e-12
    assert sum(result) == pytest.approx(1.0)
    assert result[0] <= result[1] <= result[2]  # order kept


def test_multiple_choice_unchanged_when_all_options_are_above_the_floor():
    given = options(0.2, 0.3, 0.5)
    assert floor_multiple_choice(given) == given


def _bot():
    return main.FallBot2026(
        llms={"default": "openrouter/x:free", "parser": "openrouter/x:free", "summarizer": "openrouter/x:free", "researcher": "no_research"},
        publish_reports_to_metaculus=False,
    )


def test_bot_clips_final_binary_forecast():
    question = BinaryQuestion(question_text="Will X?", id_of_post=1)
    assert asyncio.run(_bot()._aggregate_predictions([0.99, 0.99, 0.98], question)) == pytest.approx(0.97)
    assert asyncio.run(_bot()._aggregate_predictions([0.01, 0.02, 0.01], question)) == pytest.approx(0.03)


def test_bot_floors_final_multiple_choice_forecast():
    question = MultipleChoiceQuestion(question_text="Which?", id_of_post=1, options=["O0", "O1", "O2"])
    result = asyncio.run(_bot()._aggregate_predictions([options(0.0, 0.1, 0.9), options(0.0, 0.2, 0.8)], question))
    probabilities = [o.probability for o in result.predicted_options]
    assert min(probabilities) >= 0.01 - 1e-12
    assert sum(probabilities) == pytest.approx(1.0)
