"""Credits 4d: spend guards, rehearsed with replay-credits (0 model calls)."""
from __future__ import annotations

import asyncio

import pytest
from forecasting_tools.ai_models.ai_utils.response_types import TextTokenCostResponse

import bot_config
import ensemble
import main
import replay
import spend
from gemini_budget import MemoryStore, current_question_key
from tests.test_replay import QUESTIONS


async def _no_wait(seconds):
    return None


@pytest.fixture
def rehearsal(monkeypatch):
    monkeypatch.setattr(replay._RecordedAnswer, "failing_model", None)
    monkeypatch.setattr(replay._RecordedAnswer, "timeout_all", False)
    monkeypatch.setattr(asyncio, "sleep", _no_wait)
    lineup = bot_config.get_lineup("replay-credits")
    lineup.planner.seasonal_tier = "standard"
    bot = main.FallBot2026(
        llms=lineup.llms, publish_reports_to_metaculus=False, enable_summarize_research=False,
        predictions_per_research_report=lineup.predictions_per_research_report,
        required_successful_predictions=0,
    )
    bot.planner = lineup.planner
    bot.keep_records = True
    bot.question_log = None
    return lineup, bot


def _run(bot, questions=QUESTIONS):
    reports = asyncio.run(bot.forecast_questions(questions, return_exceptions=True))
    return reports, bot.__dict__.pop("kept_records", [])


# ---------------------------------------------------------------- the ledger


def test_real_cost_timeout_estimate_and_skip():
    guard = spend.SpendGuard(MemoryStore())
    current_question_key.set("7")
    guard.start(ensemble.OPUS_55)
    guard.finish(ensemble.OPUS_55, 0.12)
    assert guard.question_spent("7") == pytest.approx(0.12) and guard.day_spent() == pytest.approx(0.12)
    guard.start(ensemble.GPT_SOL)
    guard.finish(ensemble.GPT_SOL, None, timed_out=True)
    assert guard.question_spent("7") == pytest.approx(0.12 + spend.estimate_cost(ensemble.GPT_SOL))
    assert guard.refusal(ensemble.GPT_SOL) == "timed out earlier on this question"
    assert guard.in_flight["7"] == pytest.approx(0)
    guard.save()
    reloaded = spend.SpendGuard(guard._store)
    assert reloaded.question_spent("7") == pytest.approx(guard.question_spent("7"))


def test_cap_counts_calls_still_running():
    guard = spend.SpendGuard(MemoryStore())
    current_question_key.set("8")
    guard.set_cap("8", 0.40)
    assert guard.refusal(ensemble.OPUS_55) is None  # estimate ~0.19
    guard.start(ensemble.OPUS_55)
    assert guard.refusal(ensemble.OPUS_55) is None  # 0.38 <= 0.40
    guard.start(ensemble.OPUS_55)
    assert guard.refusal(ensemble.OPUS_55) == "cap"  # a third would pass 0.40


def test_real_openrouter_cost_is_recorded(rehearsal, monkeypatch):
    lineup, bot = rehearsal
    original = replay._RecordedAnswer._mockable_direct_call_to_model

    async def priced(self, prompt):
        response = await original(self, prompt)
        return TextTokenCostResponse(data=response.data, prompt_tokens_used=1, completion_tokens_used=1,
                                     total_tokens_used=2, model=self.model, cost=0.01)

    monkeypatch.setattr(replay._RecordedAnswer, "_mockable_direct_call_to_model", priced)
    _run(bot, QUESTIONS[:1])  # binary, standard: Opus, Sol paid; Flash on the free key
    guard = lineup.planner.spend
    assert guard.paid_calls >= 2
    assert guard.day_spent() == pytest.approx(0.01 * guard.paid_calls)


# ---------------------------------------------------------------- runaway


def test_runaway_every_model_times_out_stays_under_the_cap(rehearsal, monkeypatch):
    lineup, bot = rehearsal
    monkeypatch.setattr(replay._RecordedAnswer, "timeout_all", True)
    reports, _ = _run(bot)
    assert all(isinstance(r, BaseException) for r in reports)  # never a guess
    guard = lineup.planner.spend
    assert guard.caps and guard.refused > 0
    for question, cap in guard.caps.items():
        assert 0 < guard.question_spent(question) <= cap + 1e-9
    assert cap == pytest.approx(2 * ensemble.TIERS["standard"].rough_cost)
    ok, table = main.runaway_table(guard)
    assert ok and "All under the cap: yes" in table


def test_timed_out_paid_model_is_never_tried_again_on_the_question(rehearsal, monkeypatch):
    lineup, bot = rehearsal
    calls = []
    original = replay._RecordedAnswer._mockable_direct_call_to_model

    async def opus_times_out(self, prompt):
        calls.append(self.model)
        if self.model == f"replay/{ensemble.OPUS_55}":
            import litellm

            raise litellm.Timeout(message="slow", model=self.model, llm_provider="replay")
        return await original(self, prompt)

    monkeypatch.setattr(replay._RecordedAnswer, "_mockable_direct_call_to_model", opus_times_out)
    reports, records = _run(bot, QUESTIONS[:1])
    assert not isinstance(reports[0], BaseException), reports[0]
    assert calls.count(f"replay/{ensemble.OPUS_55}") == 1  # once, then the backup (Opus 5)
    assert f"replay/{ensemble.OPUS_5}" in calls


# ---------------------------------------------------------------- never re-buy


def test_retry_run_reuses_the_finished_forecasts(rehearsal):
    lineup, bot = rehearsal
    bot.fail_submission = True
    first, _ = _run(bot)
    assert all(isinstance(r, BaseException) for r in first)
    bot.fail_submission = False
    bot.__dict__.pop("_question_records", None)  # a new run starts with new records
    guard = lineup.planner.spend
    paid_before, replies_before = guard.paid_calls, replay.ReplayLlm.calls
    second, records = _run(bot)
    assert not any(isinstance(r, BaseException) for r in second), second
    assert guard.paid_calls == paid_before and replay.ReplayLlm.calls == replies_before  # nothing bought
    assert all(f.get("reused") for r in records for f in r["forecasts"])
    binary = next(r for r in records if r["question"]["question_type"] == "binary")
    assert binary["round2"] is True and any(f["kind"] == "round2" for f in binary["forecasts"])


# ---------------------------------------------------------------- daily cap, tries


def test_daily_cap_switches_to_lean_and_alerts_once():
    guard = spend.SpendGuard(MemoryStore())
    alerts = []
    target = 0.50  # per question -> daily cap 2 x 0.50 x 12 = $12
    assert main.daily_cap_tier("standard", target, guard, lambda s, c: alerts.append(c)) == "standard"
    guard.days[spend.utc_day()] = 12.5
    assert main.daily_cap_tier("standard", target, guard, lambda s, c: alerts.append(c)) == "lean"
    assert main.daily_cap_tier("full", target, guard, lambda s, c: alerts.append(c)) == "lean"
    assert alerts == [pytest.approx(12.0)]  # one issue a day


def test_paid_models_two_tries_and_cap_is_not_retried(rehearsal, monkeypatch):
    lineup, _ = rehearsal
    chain = lineup.planner._chain(ensemble.OPUS_55)
    assert chain.allowed_tries == 2 and chain._backup.allowed_tries == 2
    guard = lineup.planner.spend
    current_question_key.set("9")
    guard.set_cap("9", 0.0)  # nothing affordable
    replay.current_question.set(QUESTIONS[0])
    with pytest.raises(spend.SpendCapReached):
        asyncio.run(chain.invoke("x"))
    assert guard.refused == 1 and guard.paid_calls == 0
