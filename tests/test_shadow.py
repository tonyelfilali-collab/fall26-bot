"""Tests for PLAN.md Step 10 (shadow.py, shadow_score.py), with worked examples."""
from __future__ import annotations

import asyncio
import math

import pytest
from forecasting_tools import BinaryQuestion, ReasonedPrediction

import main
import shadow


def test_worked_example_zero_cost_shadows():
    # Forecasts 60%, 70%, 80%: median 70%.
    s = shadow.zero_cost_shadows([0.6, 0.7, 0.8])
    # stretch-1.2: logistic(1.2 x logit(0.7)) = 0.7337
    assert s["stretch-1.2"] == pytest.approx(1 / (1 + math.exp(-1.2 * math.log(0.7 / 0.3))), abs=1e-6)
    assert s["mean"] == pytest.approx(0.7)
    assert shadow.zero_cost_shadows([]) == {}


def test_referee_is_off_until_credits():
    assert shadow.REFEREE_ENABLED is False

    async def never(prompt):
        raise AssertionError("the referee must not be called while off")

    assert asyncio.run(shadow.referee_shadow("Q?", [(0.3, "a"), (0.6, "b")], never, lambda t: 0.5)) is None


def test_worked_example_referee_candidate():
    # Referee says 90% but forecasts span 30%-60%: kept at 60%; median 40% -> (60 + 40) / 2 = 50%.
    assert shadow.referee_candidate(0.9, [0.3, 0.4, 0.6]) == pytest.approx(0.5)
    # Inside the range: referee 45%, median 40% -> 42.5%.
    assert shadow.referee_candidate(0.45, [0.3, 0.4, 0.6]) == pytest.approx(0.425)


def test_referee_when_turned_on(monkeypatch):
    monkeypatch.setattr(shadow, "REFEREE_ENABLED", True)

    async def referee(prompt):
        assert "Forecaster 1: 30%" in prompt and "because A" in prompt
        return "Probability: 45%"

    from answer_parsing import parse_binary_answer

    value = asyncio.run(shadow.referee_shadow("Q?", [(0.3, "because A"), (0.4, "because B"), (0.6, "because C")], referee, parse_binary_answer))
    assert value == pytest.approx(0.425)


def test_two_line_reason():
    text = "Intro.\n\nFirst reason.\nSecond reason.\nProbability: 40%"
    assert shadow.two_line_reason(text) == "First reason. Second reason."


class _Writer:
    def __init__(self):
        self.records = []

    def save(self, path, record):
        self.records.append(record)
        return True


def test_shadows_are_saved_but_never_submitted(monkeypatch):
    answers = iter([0.6, 0.7, 0.8])

    async def fake_forecast(self, question, prompt):
        return ReasonedPrediction(prediction_value=next(answers), reasoning="x")

    monkeypatch.setattr(main.FallBot2026, "_binary_prompt_to_forecast", fake_forecast)
    bot = main.FallBot2026(
        llms={"default": "openrouter/x:free", "parser": "openrouter/x:free", "summarizer": "openrouter/x:free", "researcher": "no_research"},
        publish_reports_to_metaculus=False, enable_summarize_research=False, predictions_per_research_report=3,
    )
    bot.question_log = _Writer()
    q = BinaryQuestion(question_text="Will X?", id_of_post=8, page_url="https://www.metaculus.com/questions/8")
    [report] = asyncio.run(bot.forecast_questions([q], return_exceptions=True))
    assert report.prediction == pytest.approx(0.7)  # the live answer: the median, unchanged
    record = bot.question_log.records[0]
    assert record["shadow"]["mean"] == pytest.approx(0.7)
    assert record["shadow"]["stretch-1.2"] > 0.7
