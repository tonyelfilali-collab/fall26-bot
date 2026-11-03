"""MiniBench burst days (frontier.py, architect 2 Nov; 3 Nov: MiniBench's OWN
spend): from $5 of the UTC day's MiniBench Claude spend (running MiniBench
reservations included) MiniBench gets plain Opus only (no agent) + Flash; plain
Opus while the WHOLE day's Claude spend is under $17 ($20 day cap - $3);
seasonal unchanged."""
from __future__ import annotations

import pytest

import frontier
from frontier import AGENT, AGENT2, OPUS_55
from gemini_budget import MemoryStore
from spend import current_question_key, question_seasonal, utc_day
from tests.test_pacing import _plan, _two


def _day(planner, dollars, running=0.0, seasonal=0.0):  # type: ignore[no-untyped-def]
    # dollars: MiniBench's own Claude spend today; seasonal: the rest of the day's.
    planner.claude.days[utc_day()] = dollars + seasonal
    planner.claude.minibench_days[utc_day()] = dollars
    planner.claude.in_flight.clear()
    if running:  # a running MiniBench question's reservation
        planner.minibench_questions.add("running")
        planner.claude.in_flight["running"] = running


@pytest.mark.parametrize("day, running, agent", [
    (0.00, 0.0, True), (4.99, 0.0, True),
    (5.00, 0.0, False), (7.50, 0.0, False),
    (4.00, 1.25, False),  # a running MiniBench agent's reservation counts, as for the $9 limit
])
def test_minibench_agent_until_5_dollars(day, running, agent):
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


@pytest.mark.parametrize("seasonal", [6.00, 11.00, 11.45])
def test_seasonal_spend_does_not_take_the_minibench_agent_or_opus(seasonal):
    # 3 Nov: a heavy seasonal day ($11.45) had cut MiniBench to no Claude at $9.
    planner, _ = _two(5.0)
    _day(planner, 0.50, seasonal=seasonal)
    assert _plan(planner, False, "56")[:2] == [AGENT, OPUS_55]
    _day(planner, 5.00, seasonal=seasonal)
    assert _plan(planner, False, "57")[0] == OPUS_55 and planner.last_plan["burst"] is True


@pytest.mark.parametrize("whole_day, claude", [(16.55, True), (16.70, False), (17.00, False)])
def test_plain_opus_until_17_dollars_of_the_whole_day(whole_day, claude):
    # $0.45 plain Opus reservation: 16.55 + 0.45 = 17.00 fits; 16.70 + 0.45 does not.
    planner, _ = _two(5.0)
    _day(planner, 6.00, seasonal=whole_day - 6.00)
    models = _plan(planner, False, "52")
    assert any(m.startswith("anthropic/") for m in models) == claude
    if claude:
        assert models[0] == OPUS_55 and AGENT not in models


def test_one_seasonal_agent_keeps_3_dollars_too():
    planner, _ = _two(5.0, agents=1)
    assert planner.claude.daily_cap == 15.00 and planner.claude.minibench_daily_cap == 12.00


def test_seasonal_keeps_both_agents_on_a_burst_day():
    planner, _ = _two(5.0)
    _day(planner, 8.50)
    assert _plan(planner, True, "53")[:2] == [AGENT, AGENT2]
    assert planner.claude.caps["53"] == frontier.AGENT2_SEASONAL_QUESTION_CAP
    assert "burst" not in planner.last_plan


def test_the_limits():
    assert frontier.CLAUDE_MINIBENCH_AGENT_DAY_LIMIT == 5.00 < frontier.CLAUDE_MINIBENCH_DAY_LIMIT == 17.00
    planner, _ = _two(5.0)
    assert planner.claude.daily_cap == 20.00 and planner.claude.minibench_daily_cap == 17.00


@pytest.mark.parametrize("flag, counted", [(False, True), (None, True), (True, False)])
def test_minibench_spend_is_counted_per_day_and_saved(flag, counted):
    planner, _ = _two(5.0)
    claude = planner.claude
    tokens = current_question_key.set("60"), question_seasonal.set(flag)
    try:
        claude.start(OPUS_55)
        claude.finish(OPUS_55, True, cost=0.25)
    finally:
        current_question_key.reset(tokens[0]), question_seasonal.reset(tokens[1])
    assert claude.day_spent() == pytest.approx(0.25)
    assert claude.minibench_day_spent() == pytest.approx(0.25 if counted else 0.0)  # unknown: MiniBench (fail closed)
    saved = claude.snapshot()
    assert saved["minibench_days"].get(utc_day(), 0.0) == pytest.approx(0.25 if counted else 0.0)
    reloaded = frontier.ClaudeBudget(MemoryStore(saved), 170.0, claude.reset, today=claude.today)
    assert reloaded.minibench_day_spent() == pytest.approx(0.25 if counted else 0.0)


def test_a_running_seasonal_question_does_not_take_the_minibench_agent():
    # Its two agents reserve $4.52: counted for the $9 limit, not for the $5 burst rule.
    planner, _ = _two(5.0)
    _day(planner, 1.00)
    planner.claude.in_flight["seasonal-running"] = 4.52
    assert _plan(planner, False, "54")[:2] == [AGENT, OPUS_55]
    # A running MiniBench agent's reservation does count.
    planner.minibench_questions.add("mini-running")
    planner.claude.in_flight["mini-running"] = 4.00
    assert AGENT not in _plan(planner, False, "55")
