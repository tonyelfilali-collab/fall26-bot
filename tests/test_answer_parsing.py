"""
Worked examples for reading answers directly (answer_parsing.py), and proof
that the bot then skips the parser model.
"""
from __future__ import annotations

import asyncio

import pytest
from forecasting_tools import BinaryQuestion, MultipleChoiceQuestion, NumericQuestion

import main
from answer_parsing import parse_binary_answer, parse_multiple_choice_answer, parse_percentile_answer
from distributions import STEP8_PERCENTILES


# ---------------------------------------------------------------- binary


@pytest.mark.parametrize(
    "text, expected",
    [
        ("...reasoning...\nProbability: 73%", 0.73),
        ("Probability: 20%\nOn reflection...\nProbability: 35%", 0.35),  # the last one wins
        ("**Probability: 12.5%**", 0.125),
        ("probability:  5 %", 0.05),
        ("I think it's likely.", None),
        ("Probability: 140%", None),
    ],
)
def test_parse_binary_answer(text, expected):
    result = parse_binary_answer(text)
    assert result == (pytest.approx(expected) if expected is not None else None)


# ---------------------------------------------------------------- multiple choice


def test_multiple_choice_percentages():
    text = "Reasoning...\nOption_A: 50%\nOption_B: 30%\nOption_C: 20%"
    result = parse_multiple_choice_answer(text, ["A", "B", "C"])
    assert [o.probability for o in result.predicted_options] == pytest.approx([0.5, 0.3, 0.2])


def test_multiple_choice_decimals_and_real_names():
    text = "Labour: 0.6\nConservative: 0.3\nReform UK: 0.1"
    result = parse_multiple_choice_answer(text, ["Labour", "Conservative", "Reform UK"])
    assert [o.probability for o in result.predicted_options] == pytest.approx([0.6, 0.3, 0.1])


def test_multiple_choice_percent_without_sign_is_rescaled():
    result = parse_multiple_choice_answer("A: 55\nB: 45", ["A", "B"])
    assert [o.probability for o in result.predicted_options] == pytest.approx([0.55, 0.45])


def test_multiple_choice_missing_option_falls_back():
    assert parse_multiple_choice_answer("A: 60%\nB: 40%", ["A", "B", "C"]) is None


def test_multiple_choice_bad_total_falls_back():
    assert parse_multiple_choice_answer("A: 60%\nB: 60%", ["A", "B"]) is None


# ---------------------------------------------------------------- numeric


NINE = "\n".join(
    f"Percentile {p}: {v}"
    for p, v in zip(["2.5", "5", "10", "25", "50", "75", "90", "95", "97.5"], [50, 80, 120, 200, 300, 420, 560, 650, 720])
)


def test_parse_percentiles():
    result = parse_percentile_answer("Reasoning...\n" + NINE, STEP8_PERCENTILES)
    assert [p.percentile for p in result] == list(STEP8_PERCENTILES)
    assert [p.value for p in result] == [50, 80, 120, 200, 300, 420, 560, 650, 720]


def test_parse_percentiles_with_commas_unit_and_labels():
    text = NINE.replace("Percentile 2.5: 50", "Percentile 2.5: 1,050 widgets (lowest number value)")
    result = parse_percentile_answer(text, STEP8_PERCENTILES, "widgets")
    assert result[0].value == 1050


def test_parse_percentiles_word_units_fall_back():
    # "1.2 million" needs unit handling: leave it to the parser model.
    text = NINE.replace("Percentile 50: 300", "Percentile 50: 1.2 million")
    assert parse_percentile_answer(text, STEP8_PERCENTILES, "widgets") is None


def test_parse_percentiles_missing_one_falls_back():
    text = NINE.replace("Percentile 97.5: 720", "")
    assert parse_percentile_answer(text, STEP8_PERCENTILES) is None


# ---------------------------------------------------------------- the bot skips the parser model


class _NoParser:
    model = "no-parser"

    async def invoke(self, prompt):
        raise AssertionError("the parser model should not be called")


class _Forecaster:
    model = "fake"

    def __init__(self, answer):
        self.answer = answer

    async def invoke(self, prompt):
        return self.answer


def _bot(answer, monkeypatch):
    bot = main.FallBot2026(
        llms={"default": "openrouter/x:free", "parser": "openrouter/x:free", "summarizer": "openrouter/x:free", "researcher": "no_research"},
        publish_reports_to_metaculus=False,
    )

    async def fail_structure_output(*args, **kwargs):
        raise AssertionError("structure_output (the parser model) should not be called")

    monkeypatch.setattr(main, "structure_output", fail_structure_output)
    monkeypatch.setattr(
        main.FallBot2026, "get_llm",
        lambda self, purpose="default", guarantee_type=None: _Forecaster(answer) if purpose == "default" else _NoParser(),
    )
    return bot


def test_binary_forecast_without_a_parser_call(monkeypatch):
    bot = _bot("Thinking...\nProbability: 42%", monkeypatch)
    result = asyncio.run(bot._binary_prompt_to_forecast(BinaryQuestion(question_text="Will X?", id_of_post=1), "prompt"))
    assert result.prediction_value == pytest.approx(0.42)


def test_multiple_choice_forecast_without_a_parser_call(monkeypatch):
    bot = _bot("A: 70%\nB: 30%", monkeypatch)
    question = MultipleChoiceQuestion(question_text="Which?", id_of_post=1, options=["A", "B"])
    result = asyncio.run(bot._multiple_choice_prompt_to_forecast(question, "prompt"))
    assert [o.probability for o in result.prediction_value.predicted_options] == pytest.approx([0.7, 0.3])


def test_numeric_forecast_without_a_parser_call(monkeypatch):
    bot = _bot(NINE, monkeypatch)
    question = NumericQuestion(
        question_text="How many?", id_of_post=1, unit_of_measure="units", lower_bound=0.0, upper_bound=1000.0,
        open_lower_bound=False, open_upper_bound=False, zero_point=None, cdf_size=201,
    )
    result = asyncio.run(bot._parse_numeric_safely(question, NINE, "instructions"))
    assert result.declared_percentiles  # built from the directly read percentiles
