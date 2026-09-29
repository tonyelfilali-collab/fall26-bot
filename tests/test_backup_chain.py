"""Build 3: Nemotron in the backup chain (architect, 29 Sep).

- Both closing within 45 min (no Flash forecast) and when every Flash model is
  out of quota (429 or ledger at 0; a 503 is not): Nemotron first, 2 forecasts
  at the same time, median; Flash-Lite (up to 2) ONLY if Nemotron gives no
  answer. Flash-Lite may use its reserve in the window, only above it earlier.
Gemini calls go to the local dummy server; Nemotron is a recorded reply.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import gemini_budget
import main
from bot_config import BACKUP_FORECAST_MODEL, GEMINI_PARSER_MODELS, _nemotron_factory, is_free_model
from replay import ReplayChainLlm, _RecordedAnswer
from tests.test_gemini_budget import dummy, make_pool  # noqa: F401  (fixture)
from tests.test_never_miss import _answer, _bot, _question

FLASH = ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash", "gemini-3.5-flash"]
FLASH_LITE_AT_RESERVE = {m: 16 for m in GEMINI_PARSER_MODELS}  # 20/day, 20% reserve


async def _no_wait(seconds):
    return None


@pytest.fixture
def nemotron(monkeypatch):
    """Recorded-reply Nemotron; `nemotron.down = True` makes every call fail."""
    monkeypatch.setattr(_RecordedAnswer, "failing_model", None)

    class Switch:
        down = False

        def factory(self):
            if self.down:
                _RecordedAnswer.failing_model = BACKUP_FORECAST_MODEL
            return _nemotron_factory(ReplayChainLlm)()

    return Switch()


def _run(dummy, monkeypatch, nemotron, minutes_to_close, used=None, flash="overloaded"):  # noqa: F811
    for m in FLASH:
        dummy.behaviour[m] = flash
    monkeypatch.setattr(main.asyncio, "sleep", _no_wait)
    monkeypatch.setattr(main.FallBot2026, "_binary_prompt_to_forecast", lambda self, q, p: _answer(self, 0.3))
    pool = make_pool(used)
    pool.nemotron = nemotron.factory
    bot = _bot(pool, None)
    close = datetime.now(timezone.utc) + timedelta(minutes=minutes_to_close)
    [report] = asyncio.run(bot.forecast_questions([_question(close_time=close)], return_exceptions=True))
    [(_, record)] = bot.question_log.records
    return report, record, pool


def _answered(record, kind):
    return [m for f in record["forecasts"] if f["kind"] == kind and f.get("status") == "ok" for m in f["answered_models"]]


def _flash_lite_calls(dummy):  # noqa: F811
    return [c for c in dummy.calls if "flash-lite" in c]


def test_all_flash_429_with_flash_lite_at_reserve_nemotron_only(dummy, monkeypatch, nemotron):  # noqa: F811
    # Google answers 429 (daily quota) for every Flash model; 2 hours to close.
    report, record, pool = _run(dummy, monkeypatch, nemotron, 120, used=FLASH_LITE_AT_RESERVE, flash="daily_quota")
    assert not isinstance(report, BaseException), report
    assert report.prediction == pytest.approx(0.3)
    assert record["emergency"] == "flash-exhausted: nemotron"
    assert _answered(record, "backup") == [BACKUP_FORECAST_MODEL] * 2  # 2 runs
    # The Flash-Lite reserve is untouched: no Flash-Lite call, counts unchanged.
    assert _flash_lite_calls(dummy) == []
    assert {m: pool.ledger.used[m] for m in GEMINI_PARSER_MODELS} == FLASH_LITE_AT_RESERVE


def test_ledger_at_zero_is_exhaustion_too(dummy, monkeypatch, nemotron):  # noqa: F811
    used = {f"gemini/{m}": 20 for m in FLASH}
    report, record, _ = _run(dummy, monkeypatch, nemotron, 120, used=used, flash="ok")
    assert not isinstance(report, BaseException), report
    assert record["emergency"] == "flash-exhausted: nemotron"
    assert not [c for c in dummy.calls if c in FLASH]  # no Flash call at all


def test_nemotron_answers_so_flash_lite_is_not_used(dummy, monkeypatch, nemotron):  # noqa: F811
    # Flash-Lite has plenty of quota, but Nemotron answered: no Flash-Lite forecast.
    report, record, pool = _run(dummy, monkeypatch, nemotron, 120, flash="daily_quota")
    assert not isinstance(report, BaseException), report
    assert record["emergency"] == "flash-exhausted: nemotron"
    assert _answered(record, "backup") == [BACKUP_FORECAST_MODEL] * 2
    assert _flash_lite_calls(dummy) == []


def test_exhausted_and_nemotron_down_uses_flash_lite_above_reserve_only(dummy, monkeypatch, nemotron):  # noqa: F811
    nemotron.down = True
    used = {**FLASH_LITE_AT_RESERVE, "gemini/gemini-3.5-flash-lite": 15}  # 1 usable left there
    report, record, pool = _run(dummy, monkeypatch, nemotron, 120, used=used, flash="daily_quota")
    assert not isinstance(report, BaseException), report
    assert record["emergency"] == "flash-exhausted: nemotron failed -> flash-lite"
    assert _answered(record, "backup") == ["gemini/gemini-3.5-flash-lite"]
    # Only the one usable request was spent; every model is now at its reserve.
    assert {m: pool.ledger.used[m] for m in GEMINI_PARSER_MODELS} == FLASH_LITE_AT_RESERVE


def test_exhausted_nemotron_down_flash_lite_at_reserve_submits_nothing(dummy, monkeypatch, nemotron):  # noqa: F811
    nemotron.down = True
    report, record, pool = _run(dummy, monkeypatch, nemotron, 120, used=FLASH_LITE_AT_RESERVE, flash="daily_quota")
    assert isinstance(report, BaseException)  # never a guess; retried next run
    assert record["emergency"] == "flash-exhausted: nemotron failed, no flash-lite"
    assert {m: pool.ledger.used[m] for m in GEMINI_PARSER_MODELS} == FLASH_LITE_AT_RESERVE


def test_window_nemotron_first(dummy, monkeypatch, nemotron):  # noqa: F811
    report, record, _ = _run(dummy, monkeypatch, nemotron, 30)
    assert not isinstance(report, BaseException), report
    assert record["emergency"] == "window: nemotron"
    assert _answered(record, "emergency") == [BACKUP_FORECAST_MODEL] * 2
    assert _flash_lite_calls(dummy) == []


def test_window_nemotron_down_flash_lite_alone_still_submits(dummy, monkeypatch, nemotron):  # noqa: F811
    nemotron.down = True
    report, record, _ = _run(dummy, monkeypatch, nemotron, 30)
    assert not isinstance(report, BaseException), report
    assert report.prediction == pytest.approx(0.3)
    assert record["emergency"] == "window: nemotron failed -> flash-lite"
    emergency = [f for f in record["forecasts"] if f["kind"] == "emergency"]
    failed = [f for f in emergency if f["status"] == "failed"]
    assert [f["planned_model"] for f in failed] == [BACKUP_FORECAST_MODEL] * 2
    assert len(_answered(record, "emergency")) == 2  # both Flash-Lite


def test_window_may_use_the_flash_lite_reserve(dummy, monkeypatch, nemotron):  # noqa: F811
    nemotron.down = True
    report, record, pool = _run(dummy, monkeypatch, nemotron, 30, used=FLASH_LITE_AT_RESERVE)
    assert not isinstance(report, BaseException), report
    assert sum(pool.ledger.used[m] for m in GEMINI_PARSER_MODELS) > sum(FLASH_LITE_AT_RESERVE.values())


def test_503_only_day_no_early_backup_chain(dummy, monkeypatch, nemotron):  # noqa: F811
    report, record, pool = _run(dummy, monkeypatch, nemotron, 120, flash="overloaded")
    assert isinstance(report, BaseException)  # normal retry schedule
    assert "emergency" not in record
    assert not [f for f in record["forecasts"] if f["kind"] in ("backup", "emergency")]
    assert not pool.flash_exhausted()
    assert _flash_lite_calls(dummy) == []


def test_nemotron_backup_is_free_and_time_limited():
    llm = _nemotron_factory()()
    assert is_free_model(llm.model) and llm.model.endswith(":free")
    assert main.BACKUP_FORECAST_TIMEOUT_SECONDS == 240


# ---------------------------------------------------------------- quota day and DST


@pytest.mark.parametrize(
    "utc, day",
    [
        # Summer time (PDT): the quota day starts at 07:00 UTC.
        (datetime(2026, 10, 31, 6, 59, tzinfo=timezone.utc), "2026-10-30"),
        (datetime(2026, 10, 31, 7, 0, tzinfo=timezone.utc), "2026-10-31"),
        # Winter time (PST, from 1 Nov 2026): it starts at 08:00 UTC.
        (datetime(2026, 11, 2, 7, 30, tzinfo=timezone.utc), "2026-11-01"),
        (datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc), "2026-11-02"),
    ],
)
def test_quota_day_follows_pacific_midnight(utc, day):
    assert gemini_budget.quota_day(utc) == day


@pytest.mark.parametrize(
    "saved_day, now, exhausted",
    [
        ("2026-10-30", datetime(2026, 10, 31, 6, 59, tzinfo=timezone.utc), True),
        ("2026-10-30", datetime(2026, 10, 31, 7, 0, tzinfo=timezone.utc), False),
        # After 1 Nov, 07:00 UTC is still the previous quota day.
        ("2026-11-01", datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc), True),
        ("2026-11-01", datetime(2026, 11, 2, 7, 59, tzinfo=timezone.utc), True),
        ("2026-11-01", datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc), False),
    ],
)
def test_flash_exhausted_resets_at_the_quota_day(monkeypatch, saved_day, now, exhausted):
    from bot_config import GEMINI_FORECAST_MODELS, GeminiPool
    from gemini_budget import MemoryStore, QuotaLedger
    from llm_throttle import RequestPacer

    real_quota_day = gemini_budget.quota_day
    # The ledger was saved on `saved_day`, when every Flash model ran out.
    monkeypatch.setattr(gemini_budget, "quota_day", lambda n=None: saved_day)
    ledger = QuotaLedger(
        MemoryStore({"day": saved_day, "used": {m: 20 for m in GEMINI_FORECAST_MODELS}}),
        daily_limits={m: 20 for m in (*GEMINI_FORECAST_MODELS, *GEMINI_PARSER_MODELS)},
        reserve_fraction=0.2,
    )
    pool = GeminiPool(ledger=ledger, pacers={m: RequestPacer(6000) for m in ledger.daily_limits})
    # Now the clock moves to `now`.
    monkeypatch.setattr(gemini_budget, "quota_day", lambda n=None: real_quota_day(n or now))
    assert pool.flash_exhausted() is exhausted


def test_real_limits_flash_lite_at_reserve_nemotron_only(dummy, monkeypatch, nemotron):  # noqa: F811
    # The live limits: Flash 20/day, Flash-Lite 400/day (reserve 80), preview in 3.1's bucket.
    import bot_config
    from gemini_budget import MemoryStore

    used = {**{f"gemini/{m}": 20 for m in FLASH}, "gemini/gemini-3.5-flash-lite": 320, "gemini/gemini-3.1-flash-lite": 320}
    pool = bot_config._gemini_pool(MemoryStore({"day": gemini_budget.quota_day(), "used": used}))
    pool.nemotron = nemotron.factory
    monkeypatch.setattr(main.asyncio, "sleep", _no_wait)
    monkeypatch.setattr(main.FallBot2026, "_binary_prompt_to_forecast", lambda self, q, p: _answer(self, 0.3))
    bot = _bot(pool, None)
    close = datetime.now(timezone.utc) + timedelta(minutes=120)
    [report] = asyncio.run(bot.forecast_questions([_question(close_time=close)], return_exceptions=True))
    [(_, record)] = bot.question_log.records
    assert not isinstance(report, BaseException), report
    assert record["emergency"] == "flash-exhausted: nemotron"
    assert pool.ledger.used == used  # no Gemini request at all
    assert dummy.calls == []
