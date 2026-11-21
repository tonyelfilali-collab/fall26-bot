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

RESET = date(2026, 11, 16)  # a test cycle 16 Nov - 15 Dec; the next starts 16 Dec 00:00 UTC
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
    assert bot_config.CLAUDE_CYCLE_RESET_DATE == "2026-11-18"
    assert frontier.FINAL_HOURS.total_seconds() == 48 * 3600


def test_now_16_nov_is_in_the_last_48_hours_of_the_live_cycle():
    # Architect, 16 Nov 00:25 UK: the live cycle ends 18 Nov 00:00 UTC, so from 16 Nov 00:00 the bot is full.
    reset = frontier.reset_date(bot_config.CLAUDE_CYCLE_RESET_DATE)
    claude = ClaudeBudget(MemoryStore({"days": {}}), BUDGET, reset, today=date(2026, 11, 16))
    claude.cycles[claude.cycle.isoformat()] = BUDGET - 4.0
    assert claude.cycle == date(2026, 10, 18)
    assert frontier.claude_mode(claude, now=datetime(2026, 11, 16, 0, 30, tzinfo=timezone.utc))[0] == "full"
    assert frontier.claude_mode(claude, now=datetime(2026, 11, 15, 23, 59, tzinfo=timezone.utc))[0] == "minimal"


# ---- Final-hours day cap (architect, 17 Nov): in the last 48 hours of a cycle the
# day cap = max($20, today's Claude spend + the cycle's dollars left); MiniBench's
# limit is max($12.50, today's spend + dollars left - one seasonal reservation ($7.50)). Outside: $25 / $12.50 (20 Nov).
from datetime import timedelta  # noqa: E402

from spend import utc_day  # noqa: E402
from tests.test_fable import _start  # noqa: E402


def _today(left: float, today_spent: float, final: bool = True) -> ClaudeBudget:
    """A budget for the real today: its cycle ends tomorrow 00:00 UTC (final) or in 10 days."""
    today = datetime.now(timezone.utc).date()
    claude = ClaudeBudget(MemoryStore({"days": {}}), BUDGET, today + timedelta(days=1 if final else 10), today=today)
    claude.cycles[claude.cycle.isoformat()] = BUDGET - left  # today's spend is part of the cycle's
    claude.days[utc_day()] = today_spent
    return claude


@pytest.fixture
def fable_and_opus_on(monkeypatch):
    monkeypatch.setattr(bot_config, "FRONTIER_FABLE_ENABLED", True)
    monkeypatch.setattr(bot_config, "FRONTIER_SEASONAL_OPUS_ENABLED", True)


def test_final_hours_cap_rises_to_spend_plus_left(fable_and_opus_on):
    claude = _today(17.59, 8.0)
    assert claude.in_final_hours() and claude.final_hours_day_cap(20.0) == pytest.approx(25.59)
    planner, line = _start(claude)
    assert planner.claude.daily_cap == pytest.approx(25.59)
    assert planner.claude.minibench_daily_cap == pytest.approx(18.09)  # $25.59 - $7.50
    assert "; day cap $25.59 (last 48 hours)" in line and "seasonal plain Opus on" in line


def test_final_hours_little_left_keeps_20(fable_and_opus_on):
    planner, line = _start(_today(3.0, 8.0))  # $8 + $3 = $11 < $25
    assert planner.claude.daily_cap == 25.0 and planner.claude.minibench_daily_cap == pytest.approx(12.50)
    assert "day cap $" not in line


def test_not_final_hours_keeps_20(fable_and_opus_on):
    claude = _today(17.59, 8.0, final=False)
    assert not claude.in_final_hours() and claude.final_hours_day_cap(20.0) == 20.0
    planner, line = _start(claude)
    assert planner.claude.daily_cap == 25.0 and planner.claude.minibench_daily_cap == pytest.approx(12.50)
    assert "day cap $" not in line


def test_final_hours_cap_never_below_base_even_over_budget():
    assert _today(-2.0, 8.0).final_hours_day_cap(20.0) == 20.0  # ledger past the budget: $20, not less


def test_final_hours_cap_is_what_the_refusal_uses(fable_and_opus_on):
    # MiniBench at $15.00 of the day: refused under the normal $12.50 limit, allowed under $18.09.
    from frontier import OPUS_55
    from gemini_budget import current_question_key
    from spend import question_seasonal

    planner, _ = _start(_today(17.59, 8.0))  # cap $25.59, MiniBench $18.09 for the whole run
    q, s = current_question_key.set("mb1"), question_seasonal.set(False)
    try:
        planner.claude.days[utc_day()] = 15.00  # later in the day
        assert planner.claude.refusal(OPUS_55) is None
        planner.claude.days[utc_day()] = 19.80  # $19.80 + Opus's $0.36 > $19.99
        assert planner.claude.refusal(OPUS_55) == "daily cap"
    finally:
        current_question_key.reset(q)
        question_seasonal.reset(s)
    # Per-question caps unchanged by the day cap.
    assert frontier.CLAUDE_SEASONAL_QUESTION_CAP == 1.50 and frontier.CLAUDE_MINIBENCH_QUESTION_CAP == 0.60


# ---- Two-agent day cap $25 with an explicit $12.50 MiniBench limit (architect, 20 Nov).
def test_two_agent_cap_25_and_explicit_minibench_limit(fable_and_opus_on):
    planner, line = _start(_today(150.0, 0.0, final=False))
    assert planner.claude.daily_cap == 25.0 and planner.claude.minibench_daily_cap == pytest.approx(12.50)
    assert frontier.seasonal_reserve(2, fable=True, opus=True) * 3 <= frontier.CLAUDE_DAILY_CAP_TWO_AGENTS
    assert "day cap $" not in line  # $25 is the base, not a final-hours raise


@pytest.mark.parametrize("left, today, minibench", [(3.0, 8.0, 12.50), (17.59, 8.0, 18.09), (12.0, 8.0, 12.50)])
def test_final_hours_minibench_limit_rule_unchanged(left, today, minibench, fable_and_opus_on):
    # max($12.50, today's spend + dollars left - $7.50): the raised cap minus one reservation when credit raises it.
    planner, _ = _start(_today(left, today))
    assert planner.claude.minibench_daily_cap == pytest.approx(minibench)
    assert planner.claude.daily_cap == pytest.approx(max(25.0, today + left))


def test_minibench_cannot_use_the_extra_5_dollars(fable_and_opus_on):
    from frontier import OPUS_55
    from gemini_budget import current_question_key
    from spend import question_seasonal

    claude = _today(150.0, 0.0, final=False)
    claude.days[utc_day()] = 12.20  # $12.80 left under the $25 cap, but only $0.30 under MiniBench's $12.50
    planner, _ = _start(claude)
    tokens = current_question_key.set("m1"), question_seasonal.set(False)
    try:
        assert planner.claude.refusal(OPUS_55) == "daily cap"  # $12.20 + $0.36 > $12.50
    finally:
        current_question_key.reset(tokens[0]), question_seasonal.reset(tokens[1])
    tokens = current_question_key.set("s1"), question_seasonal.set(True)
    try:
        assert planner.claude.refusal(OPUS_55) is None  # seasonal still has the $25 day
    finally:
        current_question_key.reset(tokens[0]), question_seasonal.reset(tokens[1])
