"""Paid-Flash fallback for gemini-free (architect, 30 Sep): OFF by default,
guards when on, and the replay rehearsal. No real calls."""
from __future__ import annotations

import pytest

import bot_config
import paid_flash_rehearsal
import spend as spend_mod
from gemini_budget import MemoryStore, current_question_key
from spend import SpendGuard


def _chain_models(llm):
    names = []
    while llm is not None:
        names.append(llm.model)
        llm = getattr(llm, "_backup", None)
    return names


def test_switched_on_only_the_paid_flash_model_is_allowed(monkeypatch):
    assert bot_config.PAID_FLASH_FALLBACK is True  # ON: Tony, 30 Sep
    lineup = bot_config.get_lineup("gemini-free")  # the guard passes: only PAID_FLASH_MODEL is paid
    assert not lineup.free_only and lineup.allowed_paid == (bot_config.PAID_FLASH_MODEL,)
    paid = {m for m in lineup.llm_model_names() if not bot_config.is_free_model(m)}
    assert paid == {bot_config.PAID_FLASH_MODEL}
    # Any other paid model is still refused: a pool whose paid slot is Opus fails the guard.
    original = bot_config.enable_paid_flash
    monkeypatch.setattr(bot_config, "enable_paid_flash",
                        lambda pool, spend: original(pool, spend, model="openrouter/anthropic/claude-opus-5.5"))
    with pytest.raises(ValueError, match="claude-opus-5.5"):
        bot_config.get_lineup("gemini-free")


def test_switched_off_no_paid_model_anywhere(monkeypatch):
    monkeypatch.setattr(bot_config, "PAID_FLASH_FALLBACK", False)
    lineup = bot_config._gemini_free_lineup()
    assert lineup.free_only and lineup.allowed_paid == () and lineup.planner.paid_flash is None
    assert all(bot_config.is_free_model(m) for m in lineup.llm_model_names())


def test_key_limit_alert_under_2_dollars_once_a_day():
    alerts = []
    notify = lambda title, body: alerts.append(title)  # noqa: E731
    guard = SpendGuard(MemoryStore({}))
    assert not spend_mod.key_limit_alert(guard, 2.50, notify)
    assert not spend_mod.key_limit_alert(guard, None, notify)  # unknown: the fail-closed checks cover it
    assert spend_mod.key_limit_alert(guard, 1.75, notify, today="2026-10-02")
    spend_mod.key_limit_alert(guard, 1.50, notify, today="2026-10-02")  # same day: no second issue
    assert len(alerts) == 1 and "1.75" in alerts[0]


def test_on_every_flash_chain_ends_with_the_paid_slot():
    pool = bot_config._gemini_pool(MemoryStore())
    bot_config.enable_paid_flash(pool, SpendGuard(MemoryStore({})))
    token = current_question_key.set("123")
    try:
        chains = [*pool.plan(seasonal=True), pool.quick_forecaster(), pool.unplanned_forecaster()]
    finally:
        current_question_key.reset(token)
    for chain in chains:
        models = _chain_models(chain)
        assert models[-1] == bot_config.PAID_FLASH_MODEL
        assert all(m.startswith("gemini/") for m in models[:-1])  # free Flash first
    assert pool.spend.caps["123"] == bot_config.PAID_FLASH_QUESTION_CAP
    assert pool.spend.daily_cap == bot_config.PAID_FLASH_DAILY_CAP
    # Flash-Lite (parser, emergency) never gets the paid slot.
    assert bot_config.PAID_FLASH_MODEL not in _chain_models(pool.parser())
    for chain in pool.emergency_forecasters():
        assert bot_config.PAID_FLASH_MODEL not in _chain_models(chain)


def test_daily_cap_and_block_refuse_paid_calls():
    guard = SpendGuard(MemoryStore({}))
    guard.daily_cap = 1.00
    token = current_question_key.set("1")
    try:
        model = bot_config.PAID_FLASH_MODEL
        assert guard.refusal(model) is None
        guard.days[spend_mod.utc_day()] = 0.99
        assert guard.refusal(model) == "daily cap"
        guard.days[spend_mod.utc_day()] = 0.0
        guard.blocked = "spend ledger could not be loaded"
        assert guard.refusal(model) == "blocked"
    finally:
        current_question_key.reset(token)


def test_fail_closed_run_check():
    alerts = []
    notify = lambda title, body: alerts.append(title)  # noqa: E731
    missing = SpendGuard(MemoryStore(None))  # ledger missing: load_failed
    assert not spend_mod.paid_fallback_guard(missing, 0.0, notify) and missing.blocked
    unreadable_key = SpendGuard(MemoryStore({}))
    assert not spend_mod.paid_fallback_guard(unreadable_key, None, notify)
    mismatch = SpendGuard(MemoryStore({}))
    day = "2026-10-01"
    mismatch.key_day_start[day] = 0.0
    assert not spend_mod.paid_fallback_guard(mismatch, 0.80, notify, today=day)  # key spent $0.80, ledger $0
    fine = SpendGuard(MemoryStore({}))
    assert spend_mod.paid_fallback_guard(fine, 0.0, notify) and fine.blocked is None
    assert len(alerts) == 3


def test_replay_rehearsal_passes():
    ok, text = paid_flash_rehearsal.report()
    assert ok, text
