"""
Tests for PLAN.md Step 3 ("Never miss"): deadline rules, the quick forecast,
per-question JSON records, and the daily health check's rules. Model calls go
to the dummy Gemini server from test_gemini_budget; no real requests.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from forecasting_tools import BinaryQuestion, ReasonedPrediction

import health_check
import main
from deadlines import planned_forecast_timeout, quick_forecast_timeout
from question_log import QuestionLogWriter
from tests.test_gemini_budget import dummy, make_pool  # noqa: F401  (fixture)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- deadlines


def test_planned_forecasts_must_finish_15_minutes_before_close():
    assert planned_forecast_timeout(NOW + timedelta(hours=1), NOW) == pytest.approx(45 * 60)
    assert planned_forecast_timeout(None, NOW) is None


def test_inside_the_last_15_minutes_uses_the_time_left():
    # 10 minutes to close: up to 8 minutes (2 kept for submitting).
    assert planned_forecast_timeout(NOW + timedelta(minutes=10), NOW) == pytest.approx(8 * 60)
    assert planned_forecast_timeout(NOW + timedelta(minutes=1), NOW) == 0


def test_quick_forecast_timeout():
    assert quick_forecast_timeout(NOW + timedelta(minutes=10), NOW) == pytest.approx(8 * 60)
    assert quick_forecast_timeout(NOW + timedelta(minutes=2, seconds=10), NOW) == 0
    assert quick_forecast_timeout(None, NOW) is None


# ---------------------------------------------------------------- bot behaviour


class FakeWriter(QuestionLogWriter):
    def __init__(self) -> None:
        super().__init__(token="unused")
        self.records: list[tuple[str, dict]] = []

    def save(self, path: str, record: dict) -> bool:
        self.records.append((path, record))
        return True


def _bot(pool, answers):
    llms = {"default": pool.unplanned_forecaster(), "parser": pool.parser(), "summarizer": pool.parser(), "researcher": "no_research"}
    bot = main.FallBot2026(
        llms=llms, publish_reports_to_metaculus=False, enable_summarize_research=False,
        predictions_per_research_report=3, required_successful_predictions=0,
    )
    bot.gemini_pool = pool
    bot.question_log = FakeWriter()
    bot.run_mode = "test_questions"
    return bot


async def _answer(bot, probability, delay=0.0):
    await bot.get_llm("default", "llm").invoke("forecast")
    await asyncio.sleep(delay)
    return ReasonedPrediction(prediction_value=probability, reasoning=f"Probability: {probability:.0%}")


def _question(**kwargs):
    return BinaryQuestion(question_text="Will X happen?", id_of_post=5, page_url="https://www.metaculus.com/questions/5", **kwargs)


def test_quick_forecast_when_every_planned_forecast_fails(dummy, monkeypatch):  # noqa: F811
    pool = make_pool()
    bot = _bot(pool, None)
    bot.fail_planned_forecasts = True
    monkeypatch.setattr(main.FallBot2026, "_binary_prompt_to_forecast", lambda self, q, p: _answer(self, 0.6))
    [report] = asyncio.run(bot.forecast_questions([_question()], return_exceptions=True))
    assert not isinstance(report, BaseException), report
    assert report.prediction == pytest.approx(0.6)
    [(path, record)] = bot.question_log.records
    kinds = [(f["kind"], f["status"]) for f in record["forecasts"]]
    assert kinds.count(("planned", "failed")) == 3 and ("quick", "ok") in kinds


def test_quick_forecast_retries_after_overload(dummy, monkeypatch):  # noqa: F811
    pool = make_pool()
    bot = _bot(pool, None)
    bot.fail_planned_forecasts = True
    models = ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash", "gemini-3.5-flash"]
    for m in models:
        dummy.behaviour[m] = "overloaded"

    async def wait_then_recover(seconds):
        for m in models:
            dummy.behaviour[m] = "ok"

    monkeypatch.setattr(main.asyncio, "sleep", wait_then_recover)
    monkeypatch.setattr(main.FallBot2026, "_binary_prompt_to_forecast", lambda self, q, p: _answer(self, 0.7))
    [report] = asyncio.run(bot.forecast_questions([_question()], return_exceptions=True))
    assert not isinstance(report, BaseException), report
    assert report.prediction == pytest.approx(0.7)
    [(_, record)] = bot.question_log.records
    assert [f["status"] for f in record["forecasts"] if f["kind"] == "quick"] == ["failed", "ok"]


def test_slow_forecasts_are_cut_off_and_the_finished_ones_submitted(dummy, monkeypatch):  # noqa: F811
    pool = make_pool()
    bot = _bot(pool, None)
    delays = iter([0.0, 0.0, 5.0])  # the third forecast is too slow
    values = iter([0.2, 0.4, 0.9])
    monkeypatch.setattr(main.FallBot2026, "_binary_prompt_to_forecast", lambda self, q, p: _answer(self, next(values), next(delays)))
    monkeypatch.setattr(main, "planned_forecast_timeout", lambda close_time: 1.0)
    [report] = asyncio.run(bot.forecast_questions([_question()], return_exceptions=True))
    assert not isinstance(report, BaseException), report
    assert report.prediction == pytest.approx(0.3)  # median of the two that finished
    [(_, record)] = bot.question_log.records
    assert sorted(f["status"] for f in record["forecasts"]) == ["failed", "ok", "ok"]


def test_question_record_has_everything(dummy, monkeypatch):  # noqa: F811
    pool = make_pool()
    bot = _bot(pool, None)
    monkeypatch.setattr(main.FallBot2026, "_binary_prompt_to_forecast", lambda self, q, p: _answer(self, 0.5))
    asyncio.run(bot.forecast_questions([_question()], return_exceptions=True))
    [(path, record)] = bot.question_log.records
    assert path.startswith("questions/test_questions/") and "/5_" in path
    assert record["question"]["id_of_post"] == 5
    assert record["research"]["fetched_at"] and "text" in record["research"]
    ok = [f for f in record["forecasts"] if f["status"] == "ok"]
    assert len(ok) == 3
    assert all(f["answered_models"] and f["raw_output"] and f["parsed"] == 0.5 and "seconds" in f for f in ok)
    assert record["final_forecast"] == pytest.approx(0.5)
    assert record["finished_at"] and record["submitted"] is False


def test_log_writer_failure_never_raises(capsys):
    writer = QuestionLogWriter(token="")
    assert writer.save("questions/x.json", {"question": {"id_of_post": 9}}) is False
    assert "::warning title=question-log-failed::" in capsys.readouterr().out


# ---------------------------------------------------------------- health check rules


def _q(post, minutes_to_close, forecasted):
    return SimpleNamespace(id_of_post=post, close_time=NOW + timedelta(minutes=minutes_to_close), already_forecasted=forecasted)


def test_health_red_when_a_question_closes_soon_without_our_forecast():
    report = health_check.Report()
    health_check.check_open_questions({"seasonal": [_q(1, 30, False), _q(2, 30, True), _q(3, 300, False)]}, NOW, report)
    assert report.red == ["seasonal question 1 closes in 30 min without our forecast"]


def test_health_ok_when_everything_is_forecast():
    report = health_check.Report()
    health_check.check_open_questions({"MiniBench": [_q(1, 30, True)]}, NOW, report)
    assert not report.red and report.ok


def test_health_red_when_a_question_was_missed():
    report = health_check.Report()
    health_check.check_missed_questions({"seasonal": [_q(7, -30, False)], "MiniBench": []}, report)
    assert report.red == ["seasonal question 7 closed without our forecast (missed)"]
    ok = health_check.Report()
    health_check.check_missed_questions({"seasonal": [], "MiniBench": []}, ok)
    assert not ok.red and ok.ok


def test_health_red_without_a_successful_run_for_3_hours():
    report = health_check.Report()
    health_check.check_recent_success(NOW - timedelta(hours=4), NOW, report)
    health_check.check_recent_success(None, NOW, report)
    assert len(report.red) == 2
    ok = health_check.Report()
    health_check.check_recent_success(NOW - timedelta(minutes=20), NOW, ok)
    assert not ok.red


def test_health_red_when_gemini_quota_is_low():
    report = health_check.Report()
    health_check.check_gemini_quota({"day": "2026-09-28", "used": {"gemini/gemini-3.8-flash": 17}}, "2026-09-28", report)
    assert report.red and "3.8-flash 3/20" in report.red[0]
    fresh = health_check.Report()
    # Yesterday's counts don't count today.
    health_check.check_gemini_quota({"day": "2026-09-27", "used": {"gemini/gemini-3.8-flash": 20}}, "2026-09-28", fresh)
    assert not fresh.red


def test_health_asknews_402_is_a_warning_not_red():
    report = health_check.Report()
    health_check.check_asknews_status(402, report)
    assert report.warnings and not report.red


def test_health_warns_on_log_failures():
    report = health_check.Report()
    health_check.check_log_failures(["question-log-failed", "other"], report)
    assert report.warnings and not report.red


def test_health_simulated_miss_is_red(monkeypatch):
    for name in ("missed_questions", "open_questions", "last_successful_tournament_run", "asknews_status", "recent_annotation_titles"):
        monkeypatch.setattr(health_check, name, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setenv("DATA_REPO_TOKEN", "x")
    report = health_check.run(simulate_miss=True)
    assert any("SIMULATED" in p for p in report.red)


@pytest.mark.parametrize(
    "cron, when, expected",
    [
        ("0 6 * * *", datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc), True),   # summer: 07:00 BST
        ("0 7 * * *", datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc), False),
        ("0 7 * * *", datetime(2026, 11, 28, 7, 0, tzinfo=timezone.utc), True),  # winter: 07:00 GMT
        ("0 6 * * *", datetime(2026, 11, 28, 6, 0, tzinfo=timezone.utc), False),
    ],
)
def test_health_runs_at_7am_uk_time(cron, when, expected):
    assert health_check.is_uk_7am_trigger(cron, when) is expected
