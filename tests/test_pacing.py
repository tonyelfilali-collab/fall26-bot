"""Budget pacing (frontier.py, architect 22 Oct): the line, paced-1 / paced-2 and the return to full."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

import frontier
from frontier import AGENT, AGENT2, OPUS_55, ClaudeBudget
from gemini_budget import MemoryStore, current_question_key
from tests.test_frontier_agent import _planner


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _budget(over: float) -> ClaudeBudget:
    """A cycle that started today (UTC): the line is at most $170 / 28, so
    `over` dollars above it never reaches a step-down."""
    today = _now().date()
    claude = ClaudeBudget(MemoryStore({"days": {}}), 170.0, today, today=today)
    line = claude.pacing()[2]
    claude.cycles[claude.cycle.isoformat()] = line + over
    return claude


def _plan(planner, seasonal, question):
    token = current_question_key.set(question)
    try:
        return [c.model for c in planner.plan(seasonal)]
    finally:
        current_question_key.reset(token)


def test_the_line():
    start, end = date(2026, 10, 18), date(2026, 11, 18)
    assert frontier.cycle_end(start, date(2026, 11, 18)) == end
    assert frontier.pace_line(170.0, start, end, datetime(2026, 10, 18, tzinfo=timezone.utc)) == 0.0
    assert frontier.pace_line(170.0, start, end, datetime(2026, 11, 2, 12, tzinfo=timezone.utc)) == pytest.approx(85.0)
    assert frontier.pace_line(170.0, start, end, datetime(2026, 12, 1, tzinfo=timezone.utc)) == 170.0
    assert frontier.pace_line(170.0, start, end, datetime(2026, 10, 1, tzinfo=timezone.utc)) == 0.0
    # Short months and the year end.
    assert frontier.cycle_end(date(2027, 1, 31), date(2026, 10, 31)) == date(2027, 2, 28)
    assert frontier.cycle_end(date(2026, 12, 18), date(2026, 11, 18)) == date(2027, 1, 18)


@pytest.mark.parametrize("spent, mode", [
    (50.00, "full"), (60.00, "full"),          # at line + $10: still full
    (60.01, "paced-1"), (70.00, "paced-1"),    # at line + $20: still paced-1
    (70.01, "paced-2"), (120.00, "paced-2"),
    (59.99, "full"),                           # back to full at or under line + $10
])
def test_thresholds(spent, mode):
    assert frontier.pace_mode(spent, 50.00) == mode


def test_pacing_from_the_ledger():
    claude = ClaudeBudget(MemoryStore({"days": {}}), 170.0, date(2026, 11, 18), today=date(2026, 10, 22))
    claude.cycles["2026-10-18"] = 40.0
    mode, spent, line, gap = claude.pacing(datetime(2026, 10, 22, 12, tzinfo=timezone.utc))
    assert line == pytest.approx(170.0 * 4.5 / 31) and spent == 40.0 and gap == pytest.approx(40.0 - line)
    assert mode == "paced-1"  # $40 vs a $24.68 line: $15.32 over
    assert ClaudeBudget(MemoryStore({"days": {}}), 170.0, None).pacing() is None


def _two(over: float, agents: int = 2):
    planner = _planner(_budget(over))
    planner.seasonal_agents = agents
    line = planner.start_run(5.0, api_key=True)
    return planner, line


def test_full():
    planner, line = _two(5.0)
    assert planner.pace == "full" and line.endswith("; pacing full; 2 seasonal agents")
    assert _plan(planner, True, "41")[:2] == [AGENT, AGENT2]
    assert _plan(planner, False, "42")[:2] == [AGENT, OPUS_55]


def test_paced_1_minibench_drops_the_agent():
    planner, line = _two(15.0)
    assert planner.pace == "paced-1" and "; pacing paced-1" in line and "$" not in line.split("pacing")[1]
    assert planner.mode == "full"
    assert _plan(planner, True, "43")[:2] == [AGENT, AGENT2]
    assert planner.claude.caps["43"] == frontier.AGENT2_SEASONAL_QUESTION_CAP
    models = _plan(planner, False, "44")
    assert models[0] == OPUS_55 and AGENT not in models and len(models) == 3 and all("flash" in m for m in models[1:])
    assert planner.claude.caps["44"] == frontier.CLAUDE_MINIBENCH_QUESTION_CAP
    assert planner.last_plan["pace"] == "paced-1" and planner.last_plan["agent"] is False


def test_paced_2_seasonal_drops_agent2():
    planner, line = _two(25.0)
    assert planner.pace == "paced-2" and "; pacing paced-2" in line
    models = _plan(planner, True, "45")
    assert models[:2] == [AGENT, OPUS_55] and AGENT2 not in models and len(models) == 4
    assert planner.claude.caps["45"] == frontier.AGENT_SEASONAL_QUESTION_CAP
    models = _plan(planner, False, "46")
    assert models[0] == OPUS_55 and AGENT not in models
    # The day cap stays $20 (a backstop).
    assert planner.claude.daily_cap == frontier.CLAUDE_DAILY_CAP_TWO_AGENTS


def test_back_to_full_on_the_next_run():
    planner, _ = _two(25.0)
    assert planner.pace == "paced-2"
    planner.claude.cycles[planner.claude.cycle.isoformat()] = planner.claude.pacing()[2] + 9.0
    planner.start_run(5.0, api_key=True)
    assert planner.pace == "full" and _plan(planner, True, "47")[:2] == [AGENT, AGENT2]


def test_step_downs_still_win():
    planner, line = _two(0.0)
    planner.claude.cycles[planner.claude.cycle.isoformat()] = 0.80 * 170
    line = planner.start_run(5.0, api_key=True)
    assert planner.mode == "low" and planner.pace == "full" and "pacing" not in line
    assert AGENT not in _plan(planner, True, "48")


def test_agent_switched_off_no_pacing_line():
    planner = _planner(_budget(25.0), agent=False)
    assert "pacing" not in planner.start_run(5.0, api_key=True)
    assert planner.pace == "full"
