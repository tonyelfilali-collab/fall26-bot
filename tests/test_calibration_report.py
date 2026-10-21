"""Calibration report (calibration.py) on dummy records. No network."""
from __future__ import annotations

import math

import pytest

import calibration
import scoreboard


def logit(p: float) -> float:
    return math.log(p / (1 - p))


def test_stretch_scales_log_odds_then_clips():
    assert calibration.stretched(0.7, 1.0) == pytest.approx(0.7)
    assert logit(calibration.stretched(0.7, 1.5)) == pytest.approx(1.5 * logit(0.7))
    assert logit(calibration.stretched(0.3, 2.0)) == pytest.approx(2.0 * logit(0.3))
    assert calibration.stretched(0.95, 2.0) == 0.98  # the live 2-98% clip
    assert calibration.stretched(0.5, 1.73) == pytest.approx(0.5)


def test_best_k_and_ties():
    # Confident and right every time: the biggest stretch wins.
    rows = [(0.7, True), (0.3, False), (0.8, True)]
    assert calibration.best_k(rows) == 2.0
    # Wrong at 70%: no stretch is best.
    assert calibration.best_k([(0.7, False), (0.3, True)]) == 1.0
    # All at 50%: every k ties, the k nearer 1.0 is picked.
    assert calibration.best_k([(0.5, True), (0.5, False)]) == 1.0


def test_leave_one_out():
    rows = [(0.7, True), (0.7, True), (0.7, False)]
    scores, picks = calibration.leave_one_out(rows)
    # Without a "yes" question: one yes + one no at 70% -> k=1.0 best (log .7 + log .3 falls with k).
    # Without the "no": two yeses -> k=2.0.
    assert picks == {1.0: 2, 1.2: 0, 1.5: 0, 1.73: 0, 2.0: 1}
    assert scores[0] == pytest.approx(math.log(0.7))
    assert scores[2] == pytest.approx(math.log(1 - calibration.stretched(0.7, 2.0)))
    assert sum(picks.values()) == len(rows)


CDF = {"declared_percentiles": [
    {"value": 0, "percentile": 0.05}, {"value": 10, "percentile": 0.25},
    {"value": 20, "percentile": 0.75}, {"value": 30, "percentile": 0.95},
]}


def test_cdf_at_outcome_and_ranges():
    assert calibration.cdf_at_outcome(CDF, "15") == pytest.approx(0.5)
    assert calibration.cdf_at_outcome(CDF, "5") == pytest.approx(0.15)
    assert calibration.cdf_at_outcome(CDF, "below_lower_bound") == 0.05
    assert calibration.cdf_at_outcome(CDF, "above_upper_bound") == 0.95
    assert calibration.cdf_at_outcome(CDF, "-3") == 0.05
    assert calibration.cdf_at_outcome(CDF, "n/a") is None
    assert calibration.cdf_at_outcome({}, "5") is None
    assert calibration.where(0.5, 0.1, 0.9) == "inside"
    assert calibration.where(0.05, 0.1, 0.9) == "below"
    assert calibration.where(0.95, 0.1, 0.9) == "above"
    assert calibration.where(0.15, 0.25, 0.75) == "below"


@pytest.mark.parametrize(
    "text, template",
    [
        ("Will the S&P 500 close above 6,000 on Oct 31, 2026?", "exceed / above"),
        ("Will Bitcoin exceed $120,000 before November 1, 2026?", "exceed / above"),
        ("Will US unemployment fall below 4.0% in October 2026?", "below / fall"),
        ("Will the 10-year yield be between 4.0% and 4.5% on Oct 30?", "between"),
        ("Will SpaceX launch Starship before November 15, 2026?", "before <date>"),
        ("Will the Fall 2026 election be postponed?", "other"),
    ],
)
def test_template_of(text, template):
    assert calibration.template_of(text) == template


def test_threshold_and_current_value():
    assert calibration.threshold_of("Will Bitcoin exceed $120,000 before November 1, 2026?") == 120000
    assert calibration.threshold_of("Will unemployment fall below 4.1% in October 2026?") == pytest.approx(4.1)
    assert calibration.threshold_of("Will SpaceX launch Starship before November 15, 2026?") is None
    assert calibration.current_value_of("As of October 1, 2026, Bitcoin traded at $110,500.") == 110500
    assert calibration.current_value_of("no number here", "The index is currently 5,900.") == 5900


def record(post, kind, forecast, seasonal=True, text="", background="", **extra):
    return {
        "question": {"id_of_post": post, "question_type": kind, "question_text": text, "background_info": background},
        "seasonal": seasonal, "submitted": True, "final_forecast": forecast, **extra,
    }


def test_threshold_side():
    above = record(1, "binary", 0.3, False, "Will Bitcoin exceed $120,000 before November 1, 2026?",
                   "Bitcoin was at $110,000 as of October 1, 2026; currently $110,000.")
    below = record(2, "binary", 0.3, False, "Will gold fall below $2,000 by Oct 31?",
                   "Gold currently trades at $2,400.")
    unreadable = record(3, "binary", 0.3, False, "Will SpaceX launch Starship before November 15, 2026?")
    far = record(4, "binary", 0.3, False, "Will X exceed 5% in 2026?", "X currently stands at 3,000.")
    assert calibration.threshold_side(above) == "threshold above current"
    assert calibration.threshold_side(below) == "threshold below current"
    assert calibration.threshold_side(unreadable) is None
    assert calibration.threshold_side(far) is None


def test_group_of():
    assert calibration.group_of({"seasonal": True}) == "seasonal"
    assert calibration.group_of({"seasonal": False}) == "MiniBench"
    assert calibration.group_of({"question": {"tournament_slugs": ["minibench"]}}) == "MiniBench"
    assert calibration.group_of({"question": {}}) == "unlabelled"


def test_build_report_end_to_end():
    mc = {"predicted_options": [{"option_name": "A", "probability": 0.6}, {"option_name": "B", "probability": 0.4}]}
    records = [
        record(1, "binary", 0.7),
        record(2, "binary", 0.2),
        record(3, "numeric", CDF),
        record(4, "multiple_choice", mc),
        record(5, "binary", 0.6, False, "Will Bitcoin exceed $120,000 before Nov 1, 2026?", "Bitcoin is currently $110,000."),
        record(6, "binary", 0.4, False, "Will gold fall below $2,000 by Oct 31?"),
        record(7, "binary", 0.5),  # not resolved
        record(8, "discrete", CDF, False),
    ]
    resolutions = {1: "yes", 2: "no", 3: "above_upper_bound", 4: "B", 5: "no", 6: "yes", 7: None, 8: "15"}
    report, scored = calibration.build_report(records, resolutions.get, "2026-10-21")
    assert scored == 7
    assert "## Calibration report 2026-10-21" in report
    assert "### seasonal" in report and "### MiniBench" in report and "unlabelled" not in report
    # Seasonal binary: 1 of 2 Yes, mean forecast 45%, live total = Scoreboard's scores.
    assert "Binary: 2 resolved; resolved Yes 50.0%; our mean forecast 45.0%." in report
    live_total = scoreboard.binary_score(0.7, True) + scoreboard.binary_score(0.2, False)
    assert f"| 1 (live) | {live_total:.4f} |" in report
    assert "Leave-one-out" in report and "k=1.73" in report
    # Numeric above the range: CDF edge 0.95 -> inside 10-90? no: above both ranges.
    assert "| 10%-90% | 0 (0%) | 0 (0%) | 1 (100%) | 80% |" in report
    # Discrete at the median: inside both.
    assert "| 25%-75% | 0 (0%) | 1 (100%) | 0 (0%) | 50% |" in report
    assert "| 4 | 2 | 40.0% |" in report
    assert "| exceed / above | 1 | 0 (0%) |" in report and "| below / fall | 1 | 1 (100%) |" in report
    assert "1 of 2 question(s) could be classified" in report
    assert "| threshold above current | 1 | 0 (0%) |" in report


def test_empty_report():
    report, scored = calibration.build_report([], {}.get)
    assert scored == 0 and "No resolved questions yet." in report
