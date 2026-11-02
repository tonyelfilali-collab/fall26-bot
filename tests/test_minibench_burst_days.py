"""MiniBench burst days (frontier.py, architect 2 Nov): from $6 of the UTC day's
Claude spend (running reservations included) MiniBench gets plain Opus only
(no agent) + Flash; at $9 no Claude, as before; seasonal unchanged."""
from __future__ import annotations

import pytest

import frontier
from frontier import AGENT, AGENT2, OPUS_55
from spend import utc_day
from tests.test_pacing import _plan, _two


def _day(planner, dollars, running=0.0):  # type: ignore[no-untyped-def]
    planner.claude.days[utc_day()] = dollars
    planner.claude.in_flight.clear()
    if running:  # a running MiniBench question's reservation
        planner.minibench_questions.add("running")
        planner.claude.in_flight["running"] = running


@pytest.mark.parametrize("day, running, agent", [
    (0.00, 0.0, True), (5.99, 0.0, True),
    (6.00, 0.0, False), (7.50, 0.0, False),
    (5.00, 1.25, False),  # a running agent's reservation counts, as for the $9 limit
])
def test_minibench_agent_until_6_dollars(day, running, agent):
    planner, _ = _two(5.0)
    _day(planner, day, running)
    models = _plan(planner, False, "51")
    if agent:
        assert models[:2] == [AGENT, OPUS_55] and planner.claude.caps["51"] == frontier.AGENT_MINIBENCH_QUESTION_CAP
        assert "burst" not in planner.last_plan
    else:
        assert models[0] == OPUS_55 and AGENT not in models and len(models) == 3 and all("flash" in m for m in models[1:])
        assert planner.claude.caps["51"] == frontier.CLAUDE_MINIBENCH_QUESTION_CAP
        assert planner.last_plan["burst"] is True and planner.last_plan["agent"] is False


def test_at_9_dollars_minibench_has_no_claude_as_before():
    planner, _ = _two(5.0)
    _day(planner, 9.00)
    models = _plan(planner, False, "52")
    assert not any(m.startswith("anthropic/") for m in models)


def test_seasonal_keeps_both_agents_on_a_burst_day():
    planner, _ = _two(5.0)
    _day(planner, 8.50)
    assert _plan(planner, True, "53")[:2] == [AGENT, AGENT2]
    assert planner.claude.caps["53"] == frontier.AGENT2_SEASONAL_QUESTION_CAP
    assert "burst" not in planner.last_plan


def test_the_limits():
    assert frontier.CLAUDE_MINIBENCH_AGENT_DAY_LIMIT == 6.00 < frontier.CLAUDE_MINIBENCH_DAY_LIMIT == 9.00


def test_a_running_seasonal_question_does_not_take_the_minibench_agent():
    # Its two agents reserve $4.52: counted for the $9 limit, not for the $6 burst rule.
    planner, _ = _two(5.0)
    _day(planner, 2.00)
    planner.claude.in_flight["seasonal-running"] = 4.52
    assert _plan(planner, False, "54")[:2] == [AGENT, OPUS_55]
    # A running MiniBench agent's reservation does count.
    planner.minibench_questions.add("mini-running")
    planner.claude.in_flight["mini-running"] = 4.00
    assert AGENT not in _plan(planner, False, "55")
