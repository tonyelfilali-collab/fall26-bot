"""Zero-cost shadow variants for every question type (shadow.py), and their scoring."""
from __future__ import annotations

import asyncio
import math

import numpy as np
import pytest
from forecasting_tools import NumericQuestion

import main
import scoreboard
import shadow
from distributions import _declared_cdf, _grid, combine_numeric, pchip_distribution
from forecasting_tools import Percentile
from question_log import to_jsonable
from tests.test_distributions import options


# ---------------------------------------------------------------- binary


def test_binary_variants_listed():
    shadows = shadow.zero_cost_shadows([0.3, 0.4, 0.6])
    assert set(shadows) == {"stretch-1.2", "stretch-1.5", "mean", "geo-mean-odds", "trimmed-mean"}
    assert all(0.02 <= p <= 0.98 for p in shadows.values())


def test_stretch_15_moves_further_than_12():
    shadows = shadow.zero_cost_shadows([0.7, 0.75, 0.8])
    assert shadows["stretch-1.5"] > shadows["stretch-1.2"] > 0.75


def test_geometric_mean_of_odds():
    # odds 1/4 and 4 -> geometric mean 1 -> 50%
    assert shadow.geometric_mean_of_odds([0.2, 0.8]) == pytest.approx(0.5)
    assert shadow.zero_cost_shadows([0.2, 0.8])["geo-mean-odds"] == pytest.approx(0.5)
    # extreme values are clamped, not infinite
    assert 0 < shadow.geometric_mean_of_odds([0.0, 1.0, 1.0]) < 1


def test_trimmed_mean_drops_extremes_from_five():
    assert shadow.trimmed_mean([0.1, 0.4, 0.5, 0.6, 0.9]) == pytest.approx(0.5)
    assert shadow.trimmed_mean([0.1, 0.5, 0.9, 0.3]) == pytest.approx(0.45)  # fewer than 5: plain mean


def test_binary_invalid_forecasts_ignored():
    assert shadow.zero_cost_shadows([float("nan"), 2.0]) == {}


# ---------------------------------------------------------------- multiple choice


def test_multiple_choice_mean_and_half_percent_floor():
    raw = [[0.9, 0.1, 0.0], [0.2, 0.8, 0.0], [0.4, 0.6, 0.0]]
    predictions = [options(*r) for r in raw]  # as read live: lifted to about 1%
    shadows = shadow.multiple_choice_shadows(predictions, raw=raw)
    mean = [o["probability"] for o in shadows["mean"]["predicted_options"]]
    floor = [o["probability"] for o in shadows["floor-0.5%"]["predicted_options"]]
    assert sum(mean) == pytest.approx(1) and sum(floor) == pytest.approx(1)
    assert mean[0] == pytest.approx(0.5 * 0.99, abs=1e-6) and mean[2] == pytest.approx(0.01)
    assert floor[2] == pytest.approx(0.005)  # 0.5% floor, live uses 1%
    # Without the raw text, the read (floored) numbers are used.
    fallback = shadow.multiple_choice_shadows(predictions)
    assert fallback["floor-0.5%"]["predicted_options"][2]["probability"] > 0.009


def test_raw_multiple_choice_values_are_not_floored():
    from answer_parsing import parse_multiple_choice_answer, read_multiple_choice_values

    text = "A: 97%\nB: 3%\nC: 0%"
    assert read_multiple_choice_values(text, ["A", "B", "C"]) == pytest.approx([0.97, 0.03, 0.0])
    live = [o.probability for o in parse_multiple_choice_answer(text, ["A", "B", "C"]).predicted_options]
    assert min(live) >= 0.0099


# ---------------------------------------------------------------- numeric / discrete


def _question():
    return NumericQuestion(question_text="How many?", id_of_post=2, unit_of_measure="u", lower_bound=0.0,
                           upper_bound=100.0, open_lower_bound=False, open_upper_bound=True, zero_point=None, cdf_size=201)


def _model(center):
    heights = (0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.975)
    spread = (-30, -25, -18, -9, 0, 9, 18, 25, 30)
    return pchip_distribution([Percentile(percentile=h, value=center + d) for h, d in zip(heights, spread)], _question())


def test_numeric_mean_cdf_and_two_percent_uniform():
    q = _question()
    models = [_model(35), _model(50), _model(52)]
    shadows = shadow.numeric_shadows(models, q)
    assert set(shadows) == {"mean-cdf", "uniform-2%"}
    cdfs = np.array([_declared_cdf(m, q) for m in models])
    mean = _declared_cdf(shadows["mean-cdf"], q)
    expected = 0.95 * cdfs.mean(axis=0) + 0.05 * _grid(q)
    assert np.max(np.abs(mean - expected)) < 0.02  # the library re-standardises slightly
    live = _declared_cdf(combine_numeric(models, q), q)
    two = _declared_cdf(shadows["uniform-2%"], q)
    assert not np.allclose(two, live) and not np.allclose(mean, live)


# ---------------------------------------------------------------- saved and scored


def _bot():
    return main.FallBot2026(
        llms={"default": "openrouter/x:free", "parser": "openrouter/x:free", "summarizer": "openrouter/x:free", "researcher": "no_research"},
        publish_reports_to_metaculus=False,
    )


def test_bot_saves_shadows_for_every_type():
    from forecasting_tools import BinaryQuestion, MultipleChoiceQuestion

    bot = _bot()
    b = BinaryQuestion(question_text="Will?", id_of_post=1)
    asyncio.run(bot._aggregate_predictions([0.3, 0.5, 0.6], b))
    mc = MultipleChoiceQuestion(question_text="Which?", id_of_post=3, options=["O0", "O1", "O2"])
    asyncio.run(bot._aggregate_predictions([options(0.5, 0.3, 0.2), options(0.2, 0.4, 0.4)], mc))
    q = _question()
    asyncio.run(bot._aggregate_predictions([_model(40), _model(50)], q))
    assert set(bot._record_for(b)["shadow"]) == set(shadow.ZERO_COST_VARIANTS)
    assert set(bot._record_for(mc)["shadow"]) == set(shadow.MULTIPLE_CHOICE_VARIANTS)
    saved = bot._record_for(q)["shadow"]
    assert set(saved) == set(shadow.NUMERIC_VARIANTS)
    assert len(saved["mean-cdf"]["declared_percentiles"]) == 201  # the form the scoreboard reads


def test_scoreboard_scores_shadows_per_type():
    q = _question()
    models = [_model(40), _model(50)]
    live = to_jsonable(combine_numeric(models, q))
    records = [
        {"question": {"id_of_post": 1, "question_type": "binary"}, "final_forecast": 0.7,
         "shadow": {"stretch-1.5": 0.8}},
        {"question": {"id_of_post": 3, "question_type": "multiple_choice"},
         "final_forecast": to_jsonable(options(0.5, 0.5)),
         "shadow": {"mean": {"predicted_options": [{"option_name": "O0", "probability": 0.6},
                                                   {"option_name": "O1", "probability": 0.4}]}}},
        {"question": {"id_of_post": 2, "question_type": "numeric"}, "final_forecast": live,
         "shadow": to_jsonable(shadow.numeric_shadows(models, q))},
    ]
    resolutions = {1: "yes", 3: "O0", 2: "45"}
    scored = scoreboard.score_records(records, resolutions.get)
    report = scoreboard.report_markdown(scored)
    assert "| binary | stretch-1.5 | 1 |" in report
    assert "| multiple_choice | mean | 1 |" in report
    assert f"{math.log(0.6) - math.log(0.5):+.4f}" in report
    assert "| numeric | mean-cdf | 1 |" in report and "| numeric | uniform-2% | 1 |" in report
