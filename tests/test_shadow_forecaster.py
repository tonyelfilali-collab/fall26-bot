"""Build 1b: the free shadow forecaster (never submitted, never blocks live)."""
from __future__ import annotations

import asyncio

import pytest
from forecasting_tools import GeneralLlm

import bot_config
import main
import replay
from tests.test_replay import QUESTIONS

TYPES = ("binary", "numeric", "discrete", "multiple_choice")


def _bot(shadow_llm):
    lineup = bot_config.get_lineup("replay")
    bot = main.FallBot2026(llms=lineup.llms, publish_reports_to_metaculus=False, enable_summarize_research=False,
                           predictions_per_research_report=1)
    bot.keep_records = True
    bot.question_log = None
    bot.shadow_llm = shadow_llm
    return bot


def _run(bot):
    reports = asyncio.run(bot.forecast_questions(QUESTIONS, return_exceptions=True))
    assert not any(isinstance(r, BaseException) for r in reports), reports
    return {r["question"]["question_type"]: r for r in bot.__dict__["kept_records"]}


class _Broken(GeneralLlm):
    def __init__(self):
        super().__init__(model="openrouter/test/broken:free", temperature=None)

    async def invoke(self, prompt, system_prompt=None):
        raise RuntimeError("provider down")


class _Slow(GeneralLlm):
    def __init__(self):
        super().__init__(model="openrouter/test/slow:free", temperature=None)

    async def invoke(self, prompt, system_prompt=None):
        await asyncio.sleep(5)
        return "too late"


class _Unreadable(GeneralLlm):
    def __init__(self):
        super().__init__(model="openrouter/test/chatty:free", temperature=None)

    async def invoke(self, prompt, system_prompt=None):
        return "I think it's fairly likely, but I won't give numbers."


def test_shadow_saved_for_every_type_with_live_plus_shadow():
    bot = _bot(replay.ReplayLlm())
    records = _run(bot)
    for kind in TYPES:
        record = records[kind]
        assert record["shadow_model"]["status"] == "ok"
        assert record["reading"] == ["direct"]  # live reading unaffected
        assert record["shadow_model"]["reading"] == ["direct"]
        assert main.SHADOW_VARIANT in record["shadow"]
        assert f"live+{main.SHADOW_VARIANT}" in record["shadow"]
    assert records["binary"]["shadow"][f"live+{main.SHADOW_VARIANT}"] == pytest.approx(0.37)
    assert bot.__dict__["shadow_stats"] == {"asked": 4, "answered": 4}


def test_shadow_failure_never_touches_the_live_forecast():
    bot = _bot(_Broken())
    records = _run(bot)
    for kind in TYPES:
        assert records[kind]["shadow_model"]["status"].startswith("failed")
        assert main.SHADOW_VARIANT not in (records[kind].get("shadow") or {})
        assert records[kind]["final_forecast"] is not None
    assert bot.__dict__["shadow_stats"] == {"asked": 4, "answered": 0}


def test_shadow_timeout_is_hard(monkeypatch):
    monkeypatch.setattr(main, "SHADOW_FORECAST_TIMEOUT_SECONDS", 0.2)
    records = _run(_bot(_Slow()))
    assert all(records[k]["shadow_model"]["status"] == "timeout" for k in TYPES)
    assert all(records[k]["final_forecast"] is not None for k in TYPES)


def test_unreadable_shadow_is_dropped_without_a_parser_call(monkeypatch):
    async def parser_must_not_run(*args, **kwargs):
        raise AssertionError("the parser model was called for the shadow")

    monkeypatch.setattr(main, "structure_output", parser_must_not_run)
    records = _run(_bot(_Unreadable()))
    for kind in TYPES:
        assert records[kind]["shadow_model"]["reading"] == ["dropped"]
        assert records[kind]["shadow_model"]["status"].startswith("failed")
        assert records[kind]["reading"] == ["direct"]


def test_shadow_model_is_free():
    assert bot_config.is_free_model(bot_config.SHADOW_FORECAST_MODEL)


def test_scoreboard_answer_rate_and_variants():
    import scoreboard

    records = [
        {"question": {"question_type": "binary"}, "submitted": True, "shadow_model": {"status": "ok"}},
        {"question": {"question_type": "binary"}, "submitted": False, "shadow_model": {"status": "ok"}},
        {"question": {"question_type": "binary"}, "submitted": True, "shadow_model": {"status": "timeout"}},
        {"question": {"question_type": "numeric"}, "submitted": True},
    ]
    table = scoreboard.shadow_answer_rate(records)
    assert "| binary | 3 | 2 (67%) | 1 |" in table and "numeric" not in table
    scored = scoreboard.score_records(
        [{"question": {"id_of_post": 1, "question_type": "binary"}, "final_forecast": 0.6,
          "shadow": {main.SHADOW_VARIANT: 0.8, f"live+{main.SHADOW_VARIANT}": 0.7}}],
        {1: "yes"}.get,
    )
    report = scoreboard.report_markdown(scored)
    assert f"| binary | {main.SHADOW_VARIANT} | 1 |" in report and f"| binary | live+{main.SHADOW_VARIANT} | 1 |" in report
