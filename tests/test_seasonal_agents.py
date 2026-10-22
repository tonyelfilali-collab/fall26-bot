"""Two seasonal agents (frontier.py, item C, ON: FRONTIER_SEASONAL_AGENTS = 2): fake SDK replies, 0 real calls."""
from __future__ import annotations

import asyncio

import pytest

import bot_config
import frontier
from frontier import AGENT, AGENT2, OPUS_55
from gemini_budget import current_question_key
from spend import question_seasonal
from tests.test_frontier_agent import Fake, _budget, _fetch, _planner, _reply, _search, _text, URL_FOUND, URL_READ


def _two(claude=None, agents=2):
    planner = _planner(claude or _budget())
    planner.seasonal_agents = agents
    line = planner.start_run(5.0, api_key=True)
    return planner, line


def _plan(planner, seasonal, question):
    token = current_question_key.set(question)
    try:
        return planner.plan(seasonal)
    finally:
        current_question_key.reset(token)


def test_switched_on_live():
    assert bot_config.FRONTIER_SEASONAL_AGENTS == 2


def test_one_seasonal_agent():
    planner, line = _two(agents=1)
    assert [c.model for c in _plan(planner, True, "31")][:2] == [AGENT, OPUS_55]
    assert planner.claude.daily_cap == frontier.CLAUDE_DAILY_CAP == 15.00 and "2 seasonal agents" not in line


def test_agent2_reserve_and_the_caps():
    plain = frontier.claude_reserve(OPUS_55)
    assert frontier.agent_reserve(True, AGENT2) == pytest.approx(1.64)
    assert frontier.agent_caps(True, AGENT2) == frontier.AgentCaps(searches=4, fetches=2, output_tokens=20000, input_tokens=300000)
    # agent + agent2 + one plain Opus fallback each.
    assert frontier.agent_reserve(True) + frontier.agent_reserve(True, AGENT2) + 2 * plain <= frontier.AGENT2_SEASONAL_QUESTION_CAP + 1e-9
    assert frontier.AGENT2_SEASONAL_QUESTION_CAP == 4.70 and frontier.CLAUDE_DAILY_CAP_TWO_AGENTS == 20.00


def test_two_agents_seasonal_and_minibench_unchanged():
    planner, line = _two()
    assert "2 seasonal agents" in line
    assert planner.claude.daily_cap == 20.00 and planner.claude.minibench_daily_cap == 9.00
    chains = _plan(planner, True, "32")
    assert [c.model for c in chains][:2] == [AGENT, AGENT2] and len(chains) == 4
    assert all("flash" in c.model for c in chains[2:])
    assert planner.claude.caps["32"] == 4.70
    # Each agent has its own plain Opus fallback, with only the planned wait's time left.
    assert chains[0]._backup is not chains[1]._backup
    assert all(c._backup.model == OPUS_55 and c._backup.deadline_aware and c._backup_on_any_error for c in chains[:2])
    chains = _plan(planner, False, "33")
    assert [c.model for c in chains][:2] == [AGENT, OPUS_55] and len(chains) == 4
    assert planner.claude.caps["33"] == frontier.AGENT_MINIBENCH_QUESTION_CAP


@pytest.mark.parametrize("spent, mode", [(0.80 * 170, "low"), (0.95 * 170, "minimal")])
def test_step_downs_plain_opus_only(spent, mode):
    claude = _budget()
    claude.cycles[claude.cycle.isoformat()] = spent
    planner, _ = _two(claude)
    assert planner.mode == mode
    models = [c.model for c in _plan(planner, True, "34")]
    assert AGENT not in models and AGENT2 not in models and OPUS_55 in models


def test_agent2_refused_keeps_plain_opus_and_the_one_agent_cap(monkeypatch):
    planner, _ = _two()
    real = planner._claude_ok
    monkeypatch.setattr(planner, "_claude_ok", lambda m: m != AGENT2 and real(m))
    assert [c.model for c in _plan(planner, True, "35")][:2] == [AGENT, OPUS_55]
    assert planner.claude.caps["35"] == frontier.AGENT_SEASONAL_QUESTION_CAP


def test_agent2_call_uses_its_caps_and_both_calls_keep_their_stats(monkeypatch):
    import anthropic

    replies = [_reply([*_search(URL_FOUND), *_fetch(URL_READ), _text("Probability: 61%")], searches=1, fetches=1),
               _reply([*_search(URL_FOUND), _text("Probability: 58%")], searches=2)]
    fake = Fake(replies)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", fake)
    claude = _budget()
    planner, _ = _two(claude)

    async def go():
        current_question_key.set("36")
        question_seasonal.set(True)
        claude.set_cap("36", frontier.AGENT2_SEASONAL_QUESTION_CAP)
        frontier.forecast_deadline.set(None)
        log = []
        frontier.agent_log.set(log)
        first = await planner._agent_chain(AGENT)._mockable_direct_call_to_model("Forecast this. Probability: ZZ%")
        second = await planner._agent_chain(AGENT2)._mockable_direct_call_to_model("Forecast this. Probability: ZZ%")
        return first, second, log

    first, second, log = asyncio.run(go())
    assert (first.data, second.data) == ("Probability: 61%", "Probability: 58%")
    tools = [r["tools"] for r in fake.agent_requests()]
    assert [t["max_uses"] for t in tools[0]] == [8, 3] and [t["max_uses"] for t in tools[1]] == [4, 2]
    # Both agents get the same research lines (markets, base rate).
    assert all("Polymarket, Kalshi or Manifold" in r["system"] and "base rate" in r["system"] for r in fake.agent_requests())
    calls = claude.questions["36"]["calls"]
    assert [(c["model"], c["searches"], c["fetches"]) for c in calls] == [(AGENT, 1, 1), (AGENT2, 2, 0)]
    assert [e["model"] for e in log] == [AGENT, AGENT2] and log[1]["reserve"] == pytest.approx(1.64)
    assert claude.question_spent("36") <= frontier.AGENT2_SEASONAL_QUESTION_CAP


def test_seasonal_agents_flag_is_test_only():
    import main

    source = open(main.__file__).read()
    assert '"--seasonal-agents"' in source and "lineup.planner.seasonal_agents = args.seasonal_agents" in source
