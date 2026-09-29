"""Build 5: disagreement follow-up search (fake searches, dummy Gemini; no real calls)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from forecasting_tools import (
    MultipleChoiceQuestion,
    NumericQuestion,
    Percentile,
    PredictedOption,
    PredictedOptionList,
    ReasonedPrediction,
)

import followup
import main
from distributions import STEP8_PERCENTILES
from research import MAX_DOSSIER_TOKENS, WORDS_PER_TOKEN
from tests.test_gemini_budget import dummy, make_pool  # noqa: F401  (fixture)
from tests.test_never_miss import _bot, _question


def _numeric():
    return NumericQuestion(
        question_text="How many?", id_of_post=9, page_url="https://www.metaculus.com/questions/9",
        unit_of_measure="x", lower_bound=0.0, upper_bound=100.0, open_lower_bound=False,
        open_upper_bound=False, zero_point=None, cdf_size=201,
    )


def _dist(median):
    return SimpleNamespace(declared_percentiles=[Percentile(percentile=p, value=median + (p - 0.5) * 10) for p in STEP8_PERCENTILES])


def _mc(top):
    return PredictedOptionList(predicted_options=[
        PredictedOption(option_name="A", probability=0.6 if top == "A" else 0.2),
        PredictedOption(option_name="B", probability=0.2 if top == "A" else 0.6),
        PredictedOption(option_name="C", probability=0.2),
    ])


def test_disagreement_per_type():
    binary = _question()
    assert followup.disagreement(binary, [0.2, 0.4]) is not None  # 20 points
    assert followup.disagreement(binary, [0.3, 0.45]) is None  # exactly 15
    assert followup.disagreement(binary, [0.3]) is None
    q = _numeric()  # range 0-100
    assert followup.disagreement(q, [_dist(20), _dist(50)]) is not None  # 30% of the range
    assert followup.disagreement(q, [_dist(20), _dist(40)]) is None
    mc = MultipleChoiceQuestion(question_text="Which?", id_of_post=8, page_url="https://www.metaculus.com/questions/8", options=["A", "B", "C"])
    assert followup.disagreement(mc, [_mc("A"), _mc("B")]) == "top option differs"
    assert followup.disagreement(mc, [_mc("A"), _mc("A")]) is None


def test_reason_skips_answer_lines_and_queries_are_capped():
    text = "Long analysis.\nKey point one.\nKey point two.\nProbability: 40%"
    assert followup.reason_of(text) == "Key point one. Key point two."
    assert followup.reason_of("a\nb\nPercentile 10: 5\nPercentile 90: 9") == "a b"
    assert followup.parse_queries('Sure: {"queries": ["q1", "q2", "q3"]}') == ["q1", "q2"]
    assert followup.parse_queries("no json") == []


def test_section_is_capped_and_dossier_stays_within_6k():
    dossier = " ".join(["d"] * 5000)
    out = followup.with_followup(dossier, " ".join(["f"] * 3000))
    section = out.split(followup.SECTION_TITLE)[1]
    assert len(section.split()) <= int(followup.MAX_SECTION_TOKENS * WORDS_PER_TOKEN) + 1
    assert len(out.split()) <= int(MAX_DOSSIER_TOKENS * WORDS_PER_TOKEN)


def test_asknews_while_a_call_is_left_then_free_news():
    asked = []

    async def helper(prompt):
        return '{"queries": ["first", "second"]}'

    async def asknews(query):
        asked.append(query)
        return [SimpleNamespace(eng_title="T", summary="S", pub_date=datetime(2026, 9, 28), source_id="x")]

    from free_news import Article

    free = lambda q: [Article(published=datetime(2026, 9, 28), source="s", headline="H", snippet="x")] if q == "second" else []  # noqa: E731
    findings, detail = asyncio.run(followup.run_followup("Q?", ["r1", "r2"], helper, 1, asknews, free))
    assert asked == ["first"] and detail["asknews_calls"] == 1 and detail["free_articles"] == 1
    assert "### first" in findings and "### second" in findings


# ---------------------------------------------------------------- in the bot


async def _no_wait(seconds):
    return None


def _run_binary(dummy, monkeypatch, answers, minutes_to_close=120):  # noqa: F811
    prompts = []
    values = iter(answers)

    async def forecast(self, q, prompt):
        prompts.append(prompt)
        await self.get_llm("default", "llm").invoke("forecast")
        p = next(values)
        return ReasonedPrediction(prediction_value=p, reasoning=f"Reason A.\nReason B.\nProbability: {p:.0%}")

    async def fake_followup(question_text, reasons, invoke_helper, asknews_left, search_asknews, search_free):
        assert len(reasons) >= 2 and asknews_left == 0  # no research detail -> no AskNews call left
        return "FOLLOW-UP FACTS", {"queries": ["q"], "asknews_calls": 0, "asknews_articles": 0, "free_articles": 1}

    monkeypatch.setattr(main.asyncio, "sleep", _no_wait)
    monkeypatch.setattr(main.FallBot2026, "_binary_prompt_to_forecast", forecast)
    monkeypatch.setattr(main, "run_followup", fake_followup)
    bot = _bot(make_pool(), None)
    close = datetime.now(timezone.utc) + timedelta(minutes=minutes_to_close)
    [report] = asyncio.run(bot.forecast_questions([_question(close_time=close)], return_exceptions=True))
    [(_, record)] = bot.question_log.records
    return report, record, prompts


def test_round2_uses_the_follow_up_findings(dummy, monkeypatch):  # noqa: F811
    report, record, prompts = _run_binary(dummy, monkeypatch, [0.2, 0.6, 0.4, 0.45, 0.5])
    assert not isinstance(report, BaseException), report
    assert record["followup"]["triggered"] and record["round2"]
    round1, round2 = prompts[:3], prompts[3:]
    assert round2 and all("FOLLOW-UP FACTS" in p for p in round2)
    assert not any("FOLLOW-UP FACTS" in p for p in round1)


def test_no_follow_up_when_round_1_agrees(dummy, monkeypatch):  # noqa: F811
    report, record, prompts = _run_binary(dummy, monkeypatch, [0.4, 0.45, 0.5])
    assert record["followup"] == {"enabled": True, "triggered": False}
    assert not any("FOLLOW-UP FACTS" in p for p in prompts)


def test_skipped_within_25_minutes_of_close(dummy, monkeypatch):  # noqa: F811
    report, record, prompts = _run_binary(dummy, monkeypatch, [0.2, 0.6, 0.4, 0.45, 0.5], minutes_to_close=20)
    assert record["followup"]["skipped"] == "closes within 25 min"
    assert not any("FOLLOW-UP FACTS" in p for p in prompts)


def test_switch_off(dummy, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "FOLLOWUP_ENABLED", False)
    report, record, prompts = _run_binary(dummy, monkeypatch, [0.2, 0.6, 0.4, 0.45, 0.5])
    assert record["followup"] == {"enabled": False, "triggered": False}
