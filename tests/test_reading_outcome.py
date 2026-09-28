"""How each answer was read is logged: direct / parser / dropped."""
from __future__ import annotations

import asyncio

import pytest

import bot_config
import main
import replay
from forecast_safety import NoValidForecast
from tests.test_replay import QUESTIONS


def _bot():
    lineup = bot_config.get_lineup("replay")
    bot = main.FallBot2026(llms=lineup.llms, publish_reports_to_metaculus=False, enable_summarize_research=False,
                           predictions_per_research_report=1)
    bot.keep_records = True
    bot.question_log = None
    return bot


def test_recorded_replies_are_read_directly_for_every_type():
    bot = _bot()
    reports = asyncio.run(bot.forecast_questions(QUESTIONS, return_exceptions=True))
    assert not any(isinstance(r, BaseException) for r in reports)
    reading = {r["question"]["question_type"]: r["reading"] for r in bot.__dict__["kept_records"]}
    assert reading == {t: ["direct"] for t in ("binary", "numeric", "discrete", "multiple_choice")}


def test_parser_and_dropped(monkeypatch):
    bot = _bot()
    numeric = QUESTIONS[1]
    recorded = main.parse_percentile_answer(replay.recorded_reply(numeric), main.STEP8_PERCENTILES)

    async def parser_answers(*args, **kwargs):
        return recorded

    monkeypatch.setattr(main, "structure_output", parser_answers)
    asyncio.run(bot._parse_numeric_safely(numeric, "no percentile lines here", ""))

    async def parser_fails(*args, **kwargs):
        raise NoValidForecast("unreadable")

    monkeypatch.setattr(main, "structure_output", parser_fails)
    with pytest.raises(NoValidForecast):
        asyncio.run(bot._parse_numeric_safely(numeric, "still nothing", ""))
    assert bot._record_for(numeric)["reading"] == ["parser", "dropped"]
    # Replies that couldn't be read directly are kept (private log) to fix the reader.
    assert bot._record_for(numeric)["unread_replies"] == ["no percentile lines here", "still nothing"]
