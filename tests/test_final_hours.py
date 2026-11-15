"""Last 48 hours of the Claude cycle (architect, 15 Nov; Tony's rule: never leave
Claude credit unused at cycle end): no step-down, full until a credit error.
Before that the $12 / $5 floors and the "used up" stop apply as before."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

import bot_config
import frontier
from frontier import ClaudeBudget
from gemini_budget import MemoryStore

RESET = date(2026, 11, 16)  # cycle 16 Nov - 15 Dec; the next starts 16 Dec 00:00 UTC
BUDGET = 200.0


def _budget(left: float, today: date = date(2026, 12, 10), data: dict | None = None) -> ClaudeBudget:
    claude = ClaudeBudget(MemoryStore(data if data is not None else {"days": {}}), BUDGET, RESET, today=today)
    claude.cycles[claude.cycle.isoformat()] = BUDGET - left
    return claude


def _at(day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, 12, day, hour, minute, tzinfo=timezone.utc)


@pytest.mark.parametrize("left, mode", [(40.0, "full"), (10.0, "low"), (3.0, "minimal"), (0.0, "off")])
def test_floors_apply_with_more_than_48_hours_left(left, mode):
    # 13 Dec 23:59: 48 h 1 min left.
    assert frontier.claude_mode(_budget(left), now=_at(13, 23, 59))[0] == mode


@pytest.mark.parametrize("now", [_at(14), _at(14, 12), _at(15, 23, 59)])
@pytest.mark.parametrize("left", [40.0, 10.0, 3.0, 0.0, -2.0])
def test_last_48_hours_stay_full(now, left):
    mode, why = frontier.claude_mode(_budget(left), now=now)
    assert mode == "full"
    assert "last 48 hours of the cycle, full until the credit runs out" in why


def test_reason_names_the_dollars_left():
    assert frontier.claude_mode(_budget(10.0), now=_at(15)) == (
        "full", "$10.00 of the cycle budget left; last 48 hours of the cycle, full until the credit runs out")


def test_new_cycle_has_floors_again():
    # 16 Dec 00:00 is the next cycle (fresh budget): 30 days left, floors back on.
    claude = ClaudeBudget(MemoryStore({"days": {}}), BUDGET, RESET, today=date(2026, 12, 16))
    claude.cycles[claude.cycle.isoformat()] = BUDGET - 10.0
    assert frontier.claude_mode(claude, now=_at(16))[0] == "low"


def test_credit_error_still_switches_claude_off_in_the_last_48_hours():
    claude = _budget(10.0, today=date(2026, 12, 15))
    claude.mark_credit_error()
    assert frontier.claude_mode(claude, now=_at(15, 6)) == ("off", "credit error, a test call every hour")


def test_fail_closed_cases_unchanged_in_the_last_48_hours():
    unreadable = ClaudeBudget(MemoryStore(None), BUDGET, RESET, today=date(2026, 12, 15))
    assert frontier.claude_mode(unreadable, now=_at(15))[0] == "off"
    assert frontier.claude_mode(_budget(10.0), api_key=False, now=_at(15))[0] == "off"


def test_default_time_follows_the_budget_day():
    assert _budget(10.0, today=date(2026, 12, 15)).in_final_hours()
    assert not _budget(10.0, today=date(2026, 12, 10)).in_final_hours()
    assert frontier.claude_mode(_budget(10.0, today=date(2026, 12, 15)))[0] == "full"
    assert frontier.claude_mode(_budget(10.0, today=date(2026, 12, 10)))[0] == "low"


def test_pacing_and_day_cap_unchanged():
    claude = _budget(10.0, today=date(2026, 12, 15))
    mode, spent, line, _ = claude.pacing(now=_at(15))
    assert spent == 190.0 and line == pytest.approx(BUDGET * 29 / 30)
    assert mode == frontier.pace_mode(spent, line)
    assert claude.daily_cap == frontier.CLAUDE_DAILY_CAP


def test_live_config():
    assert bot_config.CLAUDE_BUDGET_PER_CYCLE == 200.0
    assert bot_config.CLAUDE_CYCLE_RESET_DATE == "2026-11-16"
    assert frontier.FINAL_HOURS.total_seconds() == 48 * 3600
