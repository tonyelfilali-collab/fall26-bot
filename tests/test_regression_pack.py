"""
Regression pack (Tier A, tests only): ~40 closed questions from past bot
tournaments (shapes only, tests/fixtures/past_questions.json, fetched by
fixtures_fetch.py) run through the offline answer reading, combining and
platform checks with awkward synthetic answers. Every output must either pass
the platform rules or be dropped (NoValidForecast / parse failure). Never a
guess: a kept forecast must come from the answers, not a flat default.
"""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import numpy as np
import pytest
from forecasting_tools import (
    BinaryQuestion,
    DiscreteQuestion,
    MultipleChoiceQuestion,
    NumericQuestion,
    PredictedOption,
    PredictedOptionList,
)

import main
from answer_parsing import parse_multiple_choice_answer
from forecast_safety import NoValidForecast, distribution_problems

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "past_questions.json").read_text())
HEIGHTS = ("2.5", "5", "10", "25", "50", "75", "90", "95", "97.5")


def build(shape: dict):
    fields = {k: v for k, v in shape.items() if k != "question_type"}
    kind = shape["question_type"]
    cls = {"binary": BinaryQuestion, "numeric": NumericQuestion, "discrete": DiscreteQuestion,
           "multiple_choice": MultipleChoiceQuestion}[kind]
    return cls(**fields)


QUESTIONS = [build(s) for s in FIXTURES]
NUMERIC = [q for q in QUESTIONS if isinstance(q, NumericQuestion)]
MULTIPLE_CHOICE = [q for q in QUESTIONS if isinstance(q, MultipleChoiceQuestion)]
BINARY = [q for q in QUESTIONS if isinstance(q, BinaryQuestion)]


class ParserUnavailable(Exception):
    """Stands in for the parser model: offline, a text that can't be read directly is dropped."""


@pytest.fixture
def bot(monkeypatch):
    async def no_parser(*args, **kwargs):
        raise ParserUnavailable

    monkeypatch.setattr(main, "structure_output", no_parser)
    return main.FallBot2026(
        llms={"default": "openrouter/x:free", "parser": "openrouter/x:free", "summarizer": "openrouter/x:free", "researcher": "no_research"},
        publish_reports_to_metaculus=False,
    )


def test_pack_covers_every_kind():
    assert len(QUESTIONS) >= 35
    assert {q.question_type for q in QUESTIONS} == {"binary", "numeric", "discrete", "multiple_choice"}
    assert any(q.zero_point is not None for q in NUMERIC)  # log-scaled
    assert any(q.open_lower_bound or q.open_upper_bound for q in NUMERIC)
    assert any(not (q.open_lower_bound or q.open_upper_bound) for q in NUMERIC)
    counts = {len(q.options) for q in MULTIPLE_CHOICE}
    assert min(counts) == 2 and max(counts) >= 10


# ---------------------------------------------------------------- numeric / discrete


def nominal_range(q) -> tuple[float, float]:
    lower = q.nominal_lower_bound if q.nominal_lower_bound is not None else q.lower_bound
    upper = q.nominal_upper_bound if q.nominal_upper_bound is not None else q.upper_bound
    return float(lower), float(upper)


def spread(q, positions) -> list[float]:
    lower, upper = nominal_range(q)
    if q.zero_point is not None and lower > q.zero_point:
        return [lower * (upper / lower) ** p for p in positions]
    return [lower + (upper - lower) * p for p in positions]


def text(values) -> str:
    return "\n".join(f"Percentile {h}: {v:.9g}" for h, v in zip(HEIGHTS, values))


def answers(q) -> dict[str, list[str]]:
    """Awkward synthetic answers: name -> one text per model."""
    lower, upper = nominal_range(q)
    normal = spread(q, (0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.7, 0.75, 0.8))
    low = spread(q, (0.05, 0.08, 0.12, 0.2, 0.3, 0.4, 0.5, 0.55, 0.6))
    width = upper - lower
    return {
        "identical": [text(normal)] * 3,
        "one_x1000": [text(normal), text(low), text([v * 1000 for v in normal])],
        "all_x1000": [text([v * 1000 for v in normal])] * 2,
        "on_lower_bound": [text([lower] * 5 + normal[5:]), text(normal)],
        "all_on_upper_bound": [text([upper] * 9)] * 2,
        "huge_spread": [text([lower - 50 * width + 100 * width * i / 8 for i in range(9)]), text(normal)],
        "reversed": [text(list(reversed(normal))), text(low)],
        "missing_percentile": ["\n".join(text(normal).splitlines()[:-1])],
    }


async def numeric_pipeline(bot, q, texts):
    kept = []
    for t in texts:
        try:
            kept.append(await bot._parse_numeric_safely(q, t, ""))
        except (NoValidForecast, ParserUnavailable, ValueError):
            continue  # this model's forecast is dropped (the bot drops it the same way)
    if not kept:
        raise NoValidForecast("every model dropped")
    return await bot._aggregate_predictions(kept, q)


@pytest.mark.parametrize("q", NUMERIC, ids=lambda q: f"{q.question_type}-{q.id_of_post}")
def test_numeric_outputs_pass_or_are_dropped(bot, q):
    grid = np.linspace(0, 1, q.cdf_size)
    for name, texts in answers(q).items():
        try:
            result = asyncio.run(numeric_pipeline(bot, q, texts))
        except NoValidForecast:
            continue
        assert distribution_problems(result, q) == [], f"{name}: broke the platform rules"
        cdf = np.array([p.percentile for p in result.get_cdf()])
        assert np.max(np.abs(cdf - grid)) > 0.02, f"{name}: submitted a flat (uniform) guess"


@pytest.mark.parametrize("q", NUMERIC, ids=lambda q: f"{q.question_type}-{q.id_of_post}")
def test_numeric_normal_answers_are_kept(bot, q):
    result = asyncio.run(numeric_pipeline(bot, q, answers(q)["identical"]))
    assert distribution_problems(result, q) == []


# ---------------------------------------------------------------- multiple choice


def mc_answers(q) -> dict[str, list[str]]:
    n = len(q.options)
    good = [0.55] + [0.45 / (n - 1)] * (n - 1)
    lines = lambda values, fmt: "\n".join(f"{o}: {fmt(v)}" for o, v in zip(q.options, values))  # noqa: E731
    percent = lambda v: f"{100 * v:.4f}%"  # noqa: E731
    return {
        "identical": [lines(good, percent)] * 3,
        "sums_to_0.3": [lines([0.3 * v for v in good], lambda v: f"{v:.6f}")],
        "sums_to_3": [lines([3 * v for v in good], lambda v: f"{v:.6f}")],
        "percent_sums_to_30": [lines([0.3 * v for v in good], percent)],
        "missing_option": ["\n".join(lines(good, percent).splitlines()[:-1])],
        "zeros": [lines([1.0] + [0.0] * (n - 1), percent), lines(good, percent)],
        "mixed_good_and_bad": [lines(good, percent), lines([3 * v for v in good], lambda v: f"{v:.6f}")],
    }


def read_mc(t, options):
    try:
        return parse_multiple_choice_answer(t, options)
    except ValueError:  # the model's forecast is dropped
        return None


async def mc_pipeline(bot, q, texts):
    kept = [p for p in (read_mc(t, q.options) for t in texts) if p is not None]
    if not kept:
        raise NoValidForecast("every model dropped")
    return await bot._aggregate_predictions(kept, q)


@pytest.mark.parametrize("q", MULTIPLE_CHOICE, ids=lambda q: f"mc{len(q.options)}-{q.id_of_post}")
def test_multiple_choice_outputs_pass_or_are_dropped(bot, q):
    n = len(q.options)
    for name, texts in mc_answers(q).items():
        try:
            result = asyncio.run(mc_pipeline(bot, q, texts))
        except NoValidForecast:
            continue
        probabilities = [o.probability for o in result.predicted_options]
        assert [o.option_name for o in result.predicted_options] == list(q.options), name
        assert math.isclose(sum(probabilities), 1, abs_tol=1e-6), name
        assert min(probabilities) >= 0.01 - 1e-9, name
        assert max(probabilities) - min(probabilities) > 1e-6 or n == 1, f"{name}: equal odds guess"
    # A normal answer is kept.
    asyncio.run(mc_pipeline(bot, q, mc_answers(q)["identical"]))


# ---------------------------------------------------------------- binary

BINARY_CASES = {
    "identical": [0.3, 0.3, 0.3],
    "extremes": [0.0, 1.0, 1.0],
    "invalid_mixed": [float("nan"), 1.7, 0.8],
    "huge_spread": [0.01, 0.99, 0.6],
    "all_invalid": [float("nan"), -0.2, 2.0],
}


@pytest.mark.parametrize("q", BINARY, ids=lambda q: f"binary-{q.id_of_post}")
def test_binary_outputs_pass_or_are_dropped(bot, q):
    for name, forecasts in BINARY_CASES.items():
        try:
            p = asyncio.run(bot._aggregate_predictions(forecasts, q))
        except NoValidForecast:
            assert name == "all_invalid", name
            continue
        assert 0.02 <= p <= 0.98, name
        assert p != 0.5, f"{name}: 50% guess"
