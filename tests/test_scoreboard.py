"""Worked examples for the scoreboard (scoreboard.py). No network."""
from __future__ import annotations

import math

import pytest

import scoreboard


def test_binary_score():
    assert scoreboard.binary_score(0.8, True) == pytest.approx(math.log(0.8))
    assert scoreboard.binary_score(0.8, False) == pytest.approx(math.log(0.2))
    assert math.isfinite(scoreboard.binary_score(1.0, False))


def test_multiple_choice_score():
    forecast = {"predicted_options": [{"option_name": "A", "probability": 0.6}, {"option_name": "B", "probability": 0.4}]}
    assert scoreboard.multiple_choice_score(forecast, "B") == pytest.approx(math.log(0.4))
    assert scoreboard.multiple_choice_score(forecast, "C") is None


def test_numeric_score_uses_the_bucket_the_answer_fell_in():
    # CDF 0 at 0, 0.3 at 10, 0.8 at 20, 1 at 30: the answer 15 is in (10, 20] -> mass 0.5.
    forecast = {"declared_percentiles": [
        {"value": 0, "percentile": 0.0}, {"value": 10, "percentile": 0.3},
        {"value": 20, "percentile": 0.8}, {"value": 30, "percentile": 1.0},
    ]}
    assert scoreboard.numeric_score(forecast, "15") == pytest.approx(math.log(0.5))
    assert scoreboard.numeric_score(forecast, "above_upper_bound") == pytest.approx(math.log(scoreboard.EPS))
    assert scoreboard.numeric_score(forecast, "not a number") is None


def test_score_records_and_report():
    records = [
        {"question": {"id_of_post": 1, "question_type": "binary"}, "final_forecast": 0.7, "shadow": {"mean": 0.8}},
        {"question": {"id_of_post": 2, "question_type": "binary"}, "final_forecast": 0.3, "shadow": {"mean": 0.2}},
        {"question": {"id_of_post": 3, "question_type": "multiple_choice"},
         "final_forecast": {"predicted_options": [{"option_name": "A", "probability": 0.5}, {"option_name": "B", "probability": 0.5}]}},
        {"question": {"id_of_post": 4, "question_type": "binary"}, "final_forecast": 0.5},  # not resolved yet
    ]
    resolutions = {1: "yes", 2: "no", 3: "A", 4: None}
    scored = scoreboard.score_records(records, resolutions.get)
    assert len(scored) == 3
    report = scoreboard.report_markdown(scored)
    assert "| binary | 2 |" in report and "| multiple_choice | 1 |" in report and "| all | 3 |" in report
    # mean did better on both binary questions: diff = ln .8 - ln .7
    assert f"{math.log(0.8) - math.log(0.7):+.4f}" in report


def test_empty_report():
    assert "none resolved yet" in scoreboard.report_markdown([])
