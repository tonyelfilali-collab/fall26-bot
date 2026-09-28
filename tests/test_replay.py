"""Tests for replay mode (replay.py): recorded replies, 0 model calls, real pipeline."""
from __future__ import annotations

import asyncio

import pytest
from forecasting_tools import BinaryQuestion, DiscreteQuestion, MultipleChoiceQuestion, NumericQuestion

import bot_config
import main
import replay
from answer_parsing import parse_binary_answer, parse_multiple_choice_answer, parse_percentile_answer
from distributions import STEP8_PERCENTILES


def numeric(**kw):
    fields = dict(question_text="How many?", id_of_post=2, page_url="https://www.metaculus.com/questions/2", unit_of_measure="units",
                  lower_bound=0.0, upper_bound=1000.0, open_lower_bound=False, open_upper_bound=True, zero_point=None, cdf_size=201)
    fields.update(kw)
    return NumericQuestion(**fields)


QUESTIONS = [
    BinaryQuestion(question_text="Will X?", id_of_post=1, page_url="https://www.metaculus.com/questions/1"),
    numeric(),
    DiscreteQuestion(question_text="How many?", id_of_post=3, page_url="https://www.metaculus.com/questions/3", unit_of_measure="units",
                     lower_bound=0.0, upper_bound=10.0, open_lower_bound=False, open_upper_bound=False, zero_point=None, cdf_size=11),
    MultipleChoiceQuestion(question_text="Which?", id_of_post=4, page_url="https://www.metaculus.com/questions/4", options=["Red", "Green", "Blue"]),
]


def test_recorded_replies_read_directly():
    assert parse_binary_answer(replay.recorded_reply(QUESTIONS[0])) == pytest.approx(0.37)
    mc = parse_multiple_choice_answer(replay.recorded_reply(QUESTIONS[3]), ["Red", "Green", "Blue"])
    assert [o.probability for o in mc.predicted_options] == pytest.approx([0.4, 0.3, 0.3])
    for q in QUESTIONS[1:3]:
        assert parse_percentile_answer(replay.recorded_reply(q), STEP8_PERCENTILES, q.unit_of_measure) is not None


def test_numeric_reply_is_reversed_on_purpose():
    values = [p.value for p in parse_percentile_answer(replay.recorded_reply(QUESTIONS[1]), STEP8_PERCENTILES)]
    assert values == sorted(values, reverse=True)


def test_replay_lineup_is_test_only_and_free():
    lineup = bot_config.get_lineup("replay")
    assert lineup.test_only and lineup.free_only
    assert set(lineup.llm_model_names()) == {replay.REPLAY_MODEL}


def test_replay_runs_the_pipeline_with_zero_model_calls():
    lineup = bot_config.get_lineup("replay")
    bot = main.FallBot2026(
        llms=lineup.llms, publish_reports_to_metaculus=False, enable_summarize_research=False,
        predictions_per_research_report=1,
    )
    replay.ReplayLlm.calls = 0
    reports = asyncio.run(bot.forecast_questions(QUESTIONS, return_exceptions=True))
    assert not any(isinstance(r, BaseException) for r in reports), reports
    assert replay.ReplayLlm.calls == 4  # one recorded reply per question, no model called
    assert reports[0].prediction == pytest.approx(0.37)


def test_free_model_option_only_takes_free_models():
    lineup = bot_config.get_lineup("free", free_model="openrouter/nvidia/nemotron-3-ultra-550b-a55b:free")
    assert lineup.llm_model_names()[0] == "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"
    for bad in ("openrouter/anthropic/claude-opus-5.5", "gemini/gemini-3.6-flash"):
        with pytest.raises(ValueError):
            bot_config.get_lineup("free", free_model=bad)
    with pytest.raises(ValueError):
        bot_config.get_lineup("gemini-free", free_model="openrouter/x:free")
