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


# ---- (a) reaches ONE paid Flash call before Nemotron (architect, 17 Nov 11:20 UK):
# no Claude slot and every free Flash at 0 -> the quick forecast makes one paid call.
class _Unreadable(MemoryStore):
    def load(self):  # type: ignore[no-untyped-def]
        raise OSError("test: Claude ledger unreadable")  # Claude off: no Claude slot


def _no_claude_free_zero(monkeypatch, openrouter_day=0.0, minutes=180, kinds=tuple(fr.SEASONAL_TYPES), minibench=True):  # type: ignore[no-untyped-def]
    import main

    monkeypatch.setattr(main, "QUICK_FORECAST_RETRY_WAIT_SECONDS", 0)
    seasonal = [fr._question(53000 + i, kind, minutes=minutes) for i, kind in enumerate(kinds)]
    records, planner, calls = fr.run_phase(seasonal, [fr._question(53009, "binary", minutes=minutes)] if minibench else [],
                                           frontier.ClaudeBudget(_Unreadable(), fr.BUDGET, fr.RESET),
                                           free_used_up=True, openrouter_day=openrouter_day)
    assert planner.mode == "off"
    return records, planner, calls


def test_no_claude_and_free_flash_zero_one_paid_call_not_nemotron(monkeypatch):
    records, planner, calls = _no_claude_free_zero(monkeypatch)
    assert len(records) == 5
    assert calls.count(PAID.removeprefix("replay/")) == 5  # exactly one paid call per question
    assert not any("nemotron" in c for c in calls)  # Nemotron never asked, never submitted alone
    for record in records:
        info = fr._info(record, planner)
        assert info["submitted"] and info["models"] == [PAID] and info["paid_reason"] == frontier.PAID_NO_CLAUDE
        assert record["paid_flash_quick"] is True and record["paid_flash_after_claude"] is False
        assert not record.get("emergency")
        assert info["openrouter_spent"] <= bot_config.PAID_FLASH_QUESTION_CAP + 1e-9


def test_paid_cap_refused_nemotron_and_the_hold_as_now(monkeypatch):
    import main
    from datetime import datetime, timezone

    # The paid day is used up ($1.00): the paid call is refused -> backup chain (Nemotron).
    for minutes in (60, 24 * 60):  # closes before / after the next 00:30 UTC, depending on the time now
        # One question per case: the Nemotron backup path takes ~6 s a question here (as before this change).
        records, planner, calls = _no_claude_free_zero(monkeypatch, openrouter_day=1.00, minutes=minutes,
                                                       kinds=("binary",), minibench=False)
        assert len(records) == 1
        assert PAID.removeprefix("replay/") not in calls
        for record in records:
            info = fr._info(record, planner)
            assert info["paid_flash"] == 0
            close = record["question"].get("close_time")
            close = datetime.fromisoformat(str(close)) if close else None
            held = bool(close) and main.holds_nemotron_only(close, datetime.now(timezone.utc))
            if held:  # the hold as now: not submitted, a later run retries
                assert record.get("nemotron_held") and not info["submitted"]
            else:  # Nemotron submitted, as now
                assert info["submitted"] and all("nemotron" in m for m in info["models"])
