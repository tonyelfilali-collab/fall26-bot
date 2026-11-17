"""Paid Flash only when Claude can't help (architect, 17 Nov). Paid Flash may be
called for a question only if (a) no Claude forecast is planned for it, (b) all
its Claude slots finished with nothing readable (main.py's quick forecast), or
(c) it closes within the 45-min emergency window. Fake ledgers, 0 real calls."""
from __future__ import annotations

import bot_config
import frontier
import frontier_rehearsal as fr
from gemini_budget import MemoryStore, current_question_key
from spend import SpendGuard
from tests.test_frontier_agent import _budget

PAID = bot_config.PAID_FLASH_MODEL


def _chain_models(llm):  # type: ignore[no-untyped-def]
    names = []
    while llm is not None:
        names.append(llm.model)
        llm = getattr(llm, "_backup", None)
    return names


def _planner(claude=None, api_key=True, free_used_up=False):  # type: ignore[no-untyped-def]
    planner = frontier._frontier_pool(MemoryStore(), SpendGuard(MemoryStore({"days": {}})), claude or _budget())
    if free_used_up:
        for model in bot_config.GEMINI_FORECAST_MODELS:
            planner.ledger.mark_used_up(model)
    planner.start_run(5.0, api_key=api_key)
    return planner


def _plan(planner, seasonal=True, question="q1", window=False):  # type: ignore[no-untyped-def]
    planner.paid_window = window
    token = current_question_key.set(question)
    try:
        chains = planner.plan(seasonal)
    finally:
        current_question_key.reset(token)
    models = [m for chain in chains for m in _chain_models(chain)]
    return models, planner.last_plan.get("paid_flash")


def test_claude_planned_no_paid_call():
    for free_used_up in (False, True):
        for seasonal in (True, False):
            planner = _planner(free_used_up=free_used_up)
            models, reason = _plan(planner, seasonal)
            assert any(m.startswith("anthropic/") for m in models)
            assert PAID not in models and reason == frontier.PAID_SKIPPED
            assert planner.paid_skip is False  # only while the plan is built


def test_claude_off_paid_allowed():
    planner = _planner(api_key=False)
    assert planner.mode == "off"
    models, reason = _plan(planner)
    assert PAID in models and reason == frontier.PAID_NO_CLAUDE


def test_every_claude_slot_refused_by_caps_paid_allowed():
    planner = _planner(claude=_budget(day=25.0))  # over the $20 day cap: no Claude slot fits
    models, reason = _plan(planner)
    assert not any(m.startswith("anthropic/") for m in models)
    assert PAID in models and reason == frontier.PAID_NO_CLAUDE


def test_45_minute_window_paid_allowed():
    planner = _planner()
    models, reason = _plan(planner, window=True)
    assert PAID in models and reason == frontier.PAID_WINDOW
    # Free Flash used up: the Flash slots are paid straight away, next to Claude.
    planner = _planner(free_used_up=True)
    models, reason = _plan(planner, window=True)
    assert models.count(PAID) == frontier.FLASH_FORECASTS and reason == frontier.PAID_WINDOW


def test_quick_forecast_keeps_its_paid_tail():
    # (b): main.py's quick forecast after the Claude slots: its chain still ends with paid Flash.
    planner = _planner()
    _plan(planner)
    assert _chain_models(planner.quick_forecaster())[-1] == PAID


def test_all_claude_failed_paid_allowed_after_them(monkeypatch):
    # (b) end to end on recorded replies (the rehearsal's setup: agent pinned off, no
    # quick-retry wait): free Flash at 0, every Claude call fails -> the quick
    # forecast after the Claude slots uses paid Flash; every question submitted.
    import main

    monkeypatch.setattr(bot_config, "FRONTIER_AGENT_ENABLED", False)
    monkeypatch.setattr(main, "QUICK_FORECAST_RETRY_WAIT_SECONDS", 0)
    seasonal = [fr._question(55000 + i, kind) for i, kind in enumerate(fr.SEASONAL_TYPES)]
    records, planner, _ = fr.run_phase(seasonal, [fr._question(55009, "binary")], fr._budget(), free_used_up=True,
                                       fail={m: lambda: fr._Broken("test: Claude down") for m in frontier.CLAUDE_PRICES})
    infos = [fr._info(r, planner) for r in records]
    assert len(infos) == 5
    for info in infos:
        assert info["submitted"] and info["claude"] == 0 and info["paid_flash"] >= 1
        assert info["paid_reason"] == frontier.PAID_SKIPPED and info["paid_after_claude"]
        assert info["openrouter_spent"] <= bot_config.PAID_FLASH_QUESTION_CAP + 1e-9


def test_caps_unchanged():
    assert bot_config.PAID_FLASH_QUESTION_CAP == 0.10
    assert bot_config.PAID_FLASH_DAILY_CAP == 1.00 and bot_config.PAID_FLASH_MINIBENCH_DAILY_CAP == 1.00


def test_all_claude_failed_with_the_agent_on_live_setup(monkeypatch):
    # (b) with the live setup (agent + agent2 + plain Opus + Fable all fail): paid Flash after them.
    import main

    monkeypatch.setattr(main, "QUICK_FORECAST_RETRY_WAIT_SECONDS", 0)
    records, planner, _ = fr.run_phase([fr._question(54000, "binary")], [], fr._budget(), free_used_up=True,
                                       fail={m: lambda: fr._Broken("test: Claude down") for m in frontier.CLAUDE_PRICES})
    info = fr._info(records[0], planner)
    assert planner.agent_enabled and records[0]["extra_forecasts"]["agent"] is True
    assert info["submitted"] and info["claude"] == 0 and info["paid_flash"] == 1 and info["paid_after_claude"]
