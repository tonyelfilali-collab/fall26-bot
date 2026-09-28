"""
Tests for PLAN.md Step 6 (smart ensemble), with worked examples. No network.
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest
from forecasting_tools import BinaryQuestion, ReasonedPrediction

import bot_config
import ensemble
import main
from tests.test_gemini_budget import dummy, make_pool  # noqa: F401  (fixture)


# ---------------------------------------------------------------- round-2 rule


@pytest.mark.parametrize(
    "round1, needed",
    [
        ([0.40, 0.45, 0.50], False),  # spread 10 points, median 45%: agree
        ([0.40, 0.45, 0.56], True),   # spread 16 points: disagree
        ([0.40, 0.45, 0.55], False),  # spread exactly 15 points: not over 15
        ([0.05, 0.08, 0.09], True),   # median 8%: extreme
        ([0.92, 0.93, 0.95], True),   # median 93%: extreme
        ([0.10, 0.12, 0.14], False),  # median 12%: not extreme, spread 4
        ([], False),
    ],
)
def test_round2_rule(round1, needed):
    assert ensemble.round2_needed(round1) is needed


# ---------------------------------------------------------------- spending tiers


def test_worked_example_target_spend():
    # $300 left of $500; reserve 15% of $500 = $75; ($300 - $75) / 150 questions = $1.50.
    assert ensemble.target_spend_per_question(300, 500, 150) == pytest.approx(1.50)
    assert ensemble.target_spend_per_question(50, 500, 150) == 0.0  # below the reserve
    assert ensemble.target_spend_per_question(300, 500, 0) == 0.0


@pytest.mark.parametrize(
    "target, tier",
    [(2.00, "full"), (1.02, "full"), (1.01, "standard"), (0.39, "standard"), (0.38, "lean"), (0.10, "lean")],
)
def test_choose_tier(target, tier):
    assert ensemble.choose_tier(target) == tier


def test_minibench_runs_one_tier_below():
    assert ensemble.tier_below("full") == "standard"
    assert ensemble.tier_below("standard") == "lean"
    assert ensemble.tier_below("lean") == "lean"


def test_expected_remaining_questions():
    # 6 Jan 2027 is 100 days after 28 Sep 2026: 100 x 12 per day.
    assert ensemble.expected_remaining_questions(date(2026, 9, 28)) == 100 * 12
    assert ensemble.expected_remaining_questions(date(2027, 2, 1)) == 12


def test_tier_lineups_match_the_plan():
    full = ensemble.TIERS["full"]
    assert full.binary_round1 == (ensemble.OPUS_55, ensemble.GPT_SOL, ensemble.FLASH_36)
    assert full.binary_round2 == (ensemble.FABLE_51, ensemble.OPUS_55, ensemble.FLASH_36)
    assert len(full.all_forecasters) == 6  # numeric / multiple choice: all 6
    assert ensemble.TIERS["standard"].all_forecasters.count(ensemble.FLASH_36) == 3
    assert ensemble.TIERS["lean"].all_forecasters == (ensemble.OPUS_55, ensemble.FLASH_36, ensemble.FLASH_36)


def test_backups_match_the_plan():
    assert ensemble.BACKUPS[ensemble.FABLE_51] == [ensemble.OPUS_55, ensemble.OPUS_5]
    assert ensemble.BACKUPS[ensemble.GPT_SOL] == [ensemble.GPT_55]
    assert ensemble.BACKUPS[ensemble.FLASH_36] == [ensemble.GEMINI_31_PRO]


def test_notify_tier_change_only_on_change(monkeypatch):
    sent = []
    monkeypatch.setenv("GITHUB_REPOSITORY", "x/y")
    monkeypatch.setenv("GITHUB_TOKEN", "t")

    class Ok:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(ensemble.requests, "post", lambda *a, **k: sent.append(k["json"]["title"]) or Ok())
    assert ensemble.notify_tier_change("full", "full", 1.8) is False
    assert ensemble.notify_tier_change("full", "standard", 1.2) is True
    assert sent == ["Spending tier changed: full -> standard"]


# ---------------------------------------------------------------- credits planner (off)


def test_credits_lineup_is_off_and_ready():
    assert bot_config.ACTIVE_LINEUP == "gemini-free"
    lineup = bot_config.get_lineup("credits")
    planner = lineup.planner
    # The Flash 3.6 slot starts on the free AI Studio key (4c).
    free = bot_config.CREDITS_FREE_FIRST.get
    assert [c.model for c in planner.plan(seasonal=True)] == [free(m, m) for m in ensemble.TIERS["standard"].binary_round1]
    # MiniBench one tier below (standard -> lean), numeric/MC get everyone:
    assert [c.model for c in planner.plan(seasonal=False, binary=False)] == [free(m, m) for m in ensemble.TIERS["lean"].all_forecasters]
    chain = planner.plan(seasonal=True)[0]
    assert chain.model == ensemble.OPUS_55 and chain._backup.model == ensemble.OPUS_5
    assert chain.litellm_kwargs["timeout"] == 600 and chain.allowed_tries == 2  # 4d: paid models 2 tries


# ---------------------------------------------------------------- round 2 in the bot (gemini-free, dummy server)


def _bot(pool):
    llms = {"default": pool.unplanned_forecaster(), "parser": pool.parser(), "summarizer": pool.parser(), "researcher": "no_research"}
    bot = main.FallBot2026(llms=llms, publish_reports_to_metaculus=False, enable_summarize_research=False, required_successful_predictions=0)
    bot.planner = pool
    return bot


async def _answer(bot, probability):
    await bot.get_llm("default", "llm").invoke("forecast")
    return ReasonedPrediction(prediction_value=probability, reasoning="x")


def _run(monkeypatch, pool, values):
    answers = iter(values)
    monkeypatch.setattr(main.FallBot2026, "_binary_prompt_to_forecast", lambda self, q, p: _answer(self, next(answers)))
    q = BinaryQuestion(question_text="Will X?", id_of_post=4, page_url="https://www.metaculus.com/questions/4")
    [report] = asyncio.run(_bot(pool).forecast_questions([q], return_exceptions=True))
    assert not isinstance(report, BaseException), report
    return report


def test_worked_example_round2_when_round1_disagrees(dummy, monkeypatch):  # noqa: F811
    # Round 1: 30%, 50%, 70% (spread 40 points) -> round 2 adds 2 more from
    # the unused model(s): 40%, 45%. Median of all five = 45%.
    pool = make_pool()
    report = _run(monkeypatch, pool, [0.30, 0.50, 0.70, 0.40, 0.45])
    assert report.prediction == pytest.approx(0.45)


def test_no_round2_when_round1_agrees(dummy, monkeypatch):  # noqa: F811
    pool = make_pool()
    report = _run(monkeypatch, pool, [0.40, 0.45, 0.50])
    assert report.prediction == pytest.approx(0.45)
    assert sum(pool.ledger.used.values()) == 3  # no extra calls


def test_gemini_round2_uses_only_unused_models_with_usable_budget():
    pool = make_pool()
    used = ["gemini/gemini-3.6-flash", "gemini/gemini-3.7-flash", "gemini/gemini-3.8-flash"]
    extra = pool.round2(seasonal=True, used_models=used)
    assert [c.model for c in extra] == ["gemini/gemini-3.5-flash"]
    assert not extra[0]._allow_reserve
    empty = make_pool({"gemini/gemini-3.5-flash": 16})
    assert empty.round2(seasonal=True, used_models=used) == []
