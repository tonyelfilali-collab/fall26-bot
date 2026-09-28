"""Credits rehearsal (replay-credits): the credits lineup logic with every model a replay."""
from __future__ import annotations

import asyncio

import pytest

import bot_config
import ensemble
import main
import replay
from tests.test_replay import QUESTIONS


@pytest.fixture
def rehearsal(monkeypatch):
    monkeypatch.setattr(replay._RecordedAnswer, "failing_model", None)
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


def run(bot, questions, seasonal=True):
    bot.forecasting_seasonal = seasonal
    replay.ReplayLlm.calls = 0
    reports = asyncio.run(bot.forecast_questions(questions, return_exceptions=True))
    assert not any(isinstance(r, BaseException) for r in reports), reports
    return {r["question"]["question_type"]: r for r in bot.__dict__.pop("kept_records")}


def test_lineup_is_test_only_free_and_all_replay():
    lineup = bot_config.get_lineup("replay-credits")
    assert lineup.test_only and lineup.free_only
    # Every slot is a recorded reply: the paid ones as "replay/<id>", the free
    # AI Studio ones (4c) under their Gemini name but still replay objects.
    for llm in lineup.llms.values():
        while llm is not None and not isinstance(llm, str):
            assert isinstance(llm, (replay.ReplayChainLlm, replay.ReplayLlm)), llm
            assert llm.model.startswith(("replay/", "gemini/")) or llm.model == replay.REPLAY_MODEL
            llm = getattr(llm, "_backup", None)


def test_rehearsal_credit_picks_a_real_tier():
    target = ensemble.target_spend_per_question(
        bot_config.REHEARSAL_CREDIT, bot_config.REHEARSAL_CREDIT, ensemble.expected_remaining_questions()
    )
    assert ensemble.choose_tier(target) in ensemble.TIERS


def test_seasonal_rounds_and_all_forecasters(rehearsal):
    _, bot = rehearsal
    records = run(bot, QUESTIONS)
    tier = ensemble.TIERS["standard"]
    binary = records["binary"]
    assert binary["tier"] == "standard" and binary["round2"] is True  # 37/62/30 disagree
    kinds = [f["kind"] for f in binary["forecasts"]]
    assert kinds.count("planned") == len(tier.binary_round1) and kinds.count("round2") == len(tier.binary_round2)
    for kind in ("numeric", "discrete", "multiple_choice"):
        assert len(records[kind]["forecasts"]) == len(tier.all_forecasters)
        assert records[kind]["round2"] is False
    assert all(f["answered_models"] == [f["planned_model"]] for r in records.values() for f in r["forecasts"])


def test_minibench_is_one_tier_lower(rehearsal):
    _, bot = rehearsal
    binary = run(bot, QUESTIONS[:1], seasonal=False)["binary"]
    assert binary["tier"] == "lean"
    assert [f["planned_model"] for f in binary["forecasts"] if f["kind"] == "planned"] == [
        bot_config.CREDITS_FREE_FIRST.get(m, f"replay/{m}") for m in ensemble.TIERS["lean"].binary_round1
    ]


def test_forced_failure_uses_the_backup_and_still_submits(rehearsal, monkeypatch):
    _, bot = rehearsal
    monkeypatch.setattr(replay._RecordedAnswer, "failing_model", ensemble.OPUS_55)
    records = run(bot, QUESTIONS)
    opus = [f for r in records.values() for f in r["forecasts"] if f["planned_model"] == f"replay/{ensemble.OPUS_55}"]
    assert opus and all(f["answered_models"] == [f"replay/{ensemble.OPUS_5}"] for f in opus)
    assert all(f["status"] == "ok" for r in records.values() for f in r["forecasts"])
    table = main.rehearsal_table(list(records.values()))
    assert "claude-opus-5.5 -> claude-opus-5" in table



# ---------------------------------------------------------------- 4c: free first


def _flash_answers(records):
    return [f["answered_models"] for r in records.values() for f in r["forecasts"]
            if f["planned_model"] == bot_config.CREDITS_FREE_FIRST[ensemble.FLASH_36]]


def test_flash_slot_uses_the_free_key_first(rehearsal):
    lineup, bot = rehearsal
    records = run(bot, QUESTIONS)
    answers = _flash_answers(records)
    assert answers and all(a == ["gemini/gemini-3.6-flash"] for a in answers)
    # Counted in the free ledger (in memory in the rehearsal).
    assert lineup.planner.gemini.ledger.used["gemini/gemini-3.6-flash"] == len(answers)


@pytest.mark.parametrize("error", ["429", "503"])
def test_free_key_429_or_503_goes_to_openrouter(rehearsal, monkeypatch, error):
    import litellm

    _, bot = rehearsal

    original = replay._RecordedAnswer._mockable_direct_call_to_model

    async def free_key_down(self, prompt):
        if self.model == "gemini/gemini-3.6-flash":
            if error == "429":
                raise litellm.RateLimitError(message="GenerateRequestsPerDayPerProjectPerModel", llm_provider="gemini", model=self.model)
            raise litellm.ServiceUnavailableError(message="overloaded", llm_provider="gemini", model=self.model)
        return await original(self, prompt)

    monkeypatch.setattr(replay._RecordedAnswer, "_mockable_direct_call_to_model", free_key_down)
    records = run(bot, QUESTIONS)
    answers = _flash_answers(records)
    assert answers and all(a == [f"replay/{ensemble.FLASH_36}"] for a in answers)


def test_research_parser_and_summarizer_use_the_free_flash_lite_pool():
    lineup = bot_config.get_lineup("replay-credits")
    for purpose in ("parser", "summarizer"):
        chain, models = lineup.llms[purpose], []
        while chain is not None:
            models.append(chain.model)
            chain = getattr(chain, "_backup", None)
        assert models == list(bot_config.GEMINI_PARSER_MODELS)


def test_live_credits_lineup_helpers_are_free(monkeypatch):
    monkeypatch.delenv("DATA_REPO_TOKEN", raising=False)
    lineup = bot_config.get_lineup("credits")
    assert all(bot_config.is_free_model(m) for m in (lineup.llms["parser"].model, lineup.llms["summarizer"].model))
    assert lineup.llms["researcher"] == bot_config.RESEARCHER
    flash = lineup.planner._chain(ensemble.FLASH_36)
    assert flash.model == "gemini/gemini-3.6-flash" and flash._backup.model == ensemble.FLASH_36
    assert flash._backup._backup.model == ensemble.GEMINI_31_PRO
