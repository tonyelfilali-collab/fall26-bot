"""Emergency fallback: Flash-Lite forecasters only when closing within 45 min and no Flash answered."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import main
from tests.test_gemini_budget import dummy, make_pool  # noqa: F401  (fixture)
from tests.test_never_miss import _answer, _bot, _question

FLASH = ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash", "gemini-3.5-flash"]


async def _no_wait(seconds):
    return None


def _setup(dummy, monkeypatch, minutes_to_close):  # noqa: F811
    for m in FLASH:
        dummy.behaviour[m] = "overloaded"
    monkeypatch.setattr(main.asyncio, "sleep", _no_wait)
    monkeypatch.setattr(main.FallBot2026, "_binary_prompt_to_forecast", lambda self, q, p: _answer(self, 0.3))
    bot = _bot(make_pool(), None)
    close = datetime.now(timezone.utc) + timedelta(minutes=minutes_to_close)
    return bot, _question(close_time=close)


def test_emergency_flash_lite_close_to_the_deadline(dummy, monkeypatch):  # noqa: F811
    bot, question = _setup(dummy, monkeypatch, 30)
    [report] = asyncio.run(bot.forecast_questions([question], return_exceptions=True))
    assert not isinstance(report, BaseException), report
    assert report.prediction == pytest.approx(0.3)
    [(_, record)] = bot.question_log.records
    assert record["emergency"] == "window: flash-lite"  # no Nemotron configured here
    emergency = [f for f in record["forecasts"] if f["kind"] == "emergency"]
    assert len(emergency) == 2 and all(f["status"] == "ok" for f in emergency)
    assert all(m.startswith("gemini/gemini-3.") and "flash-lite" in m for f in emergency for m in f["answered_models"])


def test_no_emergency_outside_the_window(dummy, monkeypatch):  # noqa: F811
    bot, question = _setup(dummy, monkeypatch, 120)
    [report] = asyncio.run(bot.forecast_questions([question], return_exceptions=True))
    assert isinstance(report, BaseException)  # retried by the next run, as before
    [(_, record)] = bot.question_log.records
    assert "emergency" not in record
    assert not [f for f in record["forecasts"] if f["kind"] == "emergency"]


def test_no_emergency_when_flash_answers(dummy, monkeypatch):  # noqa: F811
    bot, question = _setup(dummy, monkeypatch, 30)
    for m in FLASH:
        dummy.behaviour[m] = "ok"
    [report] = asyncio.run(bot.forecast_questions([question], return_exceptions=True))
    assert not isinstance(report, BaseException), report
    [(_, record)] = bot.question_log.records
    assert "emergency" not in record
