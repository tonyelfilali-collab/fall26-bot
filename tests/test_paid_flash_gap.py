"""Frontier (Claude on): free Flash quota at 0 -> the Flash slots are planned straight to paid Flash, within its caps."""
from __future__ import annotations

import pytest

import bot_config
import frontier
from gemini_budget import MemoryStore, current_question_key
from spend import SpendGuard, question_seasonal, utc_day
from tests.test_frontier import _budget


def _planner(used_up=(), openrouter_day: float = 0.0):
    spend = SpendGuard(MemoryStore({"days": {utc_day(): openrouter_day} if openrouter_day else {}}))
    planner = frontier._frontier_pool(MemoryStore(), spend, _budget())
    planner.agent_enabled = False
    for model in used_up:
        planner.ledger.mark_used_up(model)
    planner.start_run(5.0, api_key=True)
    return planner


def _flash(planner, seasonal=True, question="51"):
    token = current_question_key.set(question)
    try:
        return [c.model for c in planner.plan(seasonal) if not c.model.startswith("anthropic/")]
    finally:
        current_question_key.reset(token)


def test_free_quota_left_plans_free_flash():
    assert all(m in bot_config.GEMINI_FORECAST_MODELS for m in _flash(_planner()))


def test_free_quota_at_zero_plans_two_paid_flash():
    planner = _planner(used_up=bot_config.GEMINI_FORECAST_MODELS)
    assert _flash(planner) == [bot_config.PAID_FLASH_MODEL] * 2
    assert planner.last_plan["paid_flash_planned"] == 2
    assert planner.spend.caps["51"] == bot_config.PAID_FLASH_QUESTION_CAP


def test_one_free_model_left_fills_the_other_slot_with_paid():
    left = bot_config.GEMINI_FORECAST_MODELS[0]
    planner = _planner(used_up=[m for m in bot_config.GEMINI_FORECAST_MODELS if m != left])
    assert _flash(planner) == [left, bot_config.PAID_FLASH_MODEL]


@pytest.mark.parametrize("seasonal, day, refused", [(True, 0.98, "daily cap"), (False, 0.48, "daily cap"), (True, 0.48, None)])
def test_the_caps_still_refuse(seasonal, day, refused):
    planner = _planner(used_up=bot_config.GEMINI_FORECAST_MODELS, openrouter_day=day)
    token, kind = current_question_key.set("52"), question_seasonal.set(seasonal)
    try:
        planner.plan(seasonal)
        assert planner.spend.refusal(bot_config.PAID_FLASH_MODEL) == refused
    finally:
        current_question_key.reset(token), question_seasonal.reset(kind)
