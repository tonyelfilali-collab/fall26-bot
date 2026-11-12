"""Plain Fable 5.2 (frontier.FABLE; architect, 12 Nov): an extra Claude voice in
full mode, no tools, no backup; fake SDK replies, 0 real calls."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import aggregation
import bot_config
import frontier
from frontier import AGENT, AGENT2, FABLE, OPUS_55
from gemini_budget import current_question_key
from llm_throttle import NoAnswer, NoQuotaLeft
from spend import question_seasonal
from tests.test_frontier import _fake_client, _response
from tests.test_frontier_agent import Fake, _budget, _planner


@pytest.fixture(autouse=True)
def fable_on(monkeypatch):
    monkeypatch.setattr(bot_config, "FRONTIER_FABLE_ENABLED", True)


def _start(claude=None, agents=2, agent=True):
    planner = _planner(claude or _budget(), agent=agent)
    planner.seasonal_agents = agents
    line = planner.start_run(5.0, api_key=True)
    return planner, line


def _plan(planner, seasonal, question):
    token = current_question_key.set(question)
    try:
        return [c.model for c in planner.plan(seasonal)]
    finally:
        current_question_key.reset(token)


def test_switched_on_live_and_priced():
    assert "FRONTIER_FABLE_ENABLED = True" in Path(bot_config.__file__).read_text()
    assert FABLE == "anthropic/claude-fable-5-2" and frontier.CLAUDE_PRICES[FABLE] == (8.00, 40.00)
    # Worst case: 10k tokens in at $8 + 16k out (thinking included) at $40 per million.
    assert frontier.claude_reserve(FABLE) == pytest.approx(0.72)


def test_request_has_no_thinking_no_tool_choice_and_effort_high(monkeypatch):
    import anthropic

    seen: dict = {}
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _fake_client(_response(), seen))
    llm = frontier._ClaudeCall(model=FABLE, temperature=None, timeout=60, allowed_tries=1)
    reply = asyncio.run(llm._mockable_direct_call_to_model("Forecast this."))
    assert seen["model"] == "claude-fable-5-2" and seen["output_config"] == {"effort": "high"}
    for absent in ("thinking", "tool_choice", "tools", "temperature", "top_p", "top_k"):
        assert absent not in seen
    assert reply.cost == pytest.approx((7000 * 8 + 1200 * 40) / 1e6)


def test_a_refusal_is_a_failed_slot_no_retry_no_backup(monkeypatch):
    import anthropic

    fake = Fake([], plain=_response(text="", stop="refusal", out=300))
    monkeypatch.setattr(anthropic, "AsyncAnthropic", fake)
    planner, _ = _start()
    chain = planner._fable_chain()
    assert chain.model == FABLE and chain._backup is None and chain.allowed_tries == 1

    async def go():
        current_question_key.set("61")
        question_seasonal.set(True)
        planner.claude.set_cap("61", 6.02)
        return await chain.invoke("Forecast this.")

    with pytest.raises((NoQuotaLeft, NoAnswer)):
        asyncio.run(go())
    assert len(fake.plain_requests()) == 1  # one try only
    calls = planner.claude.questions["61"]["calls"]
    assert [c["model"] for c in calls] == [FABLE] and calls[0]["stop_reason"] == "refusal"
    assert calls[0]["charged"] == pytest.approx((7000 * 8 + 300 * 40) / 1e6)  # a refusal is charged its real cost


def test_seasonal_two_agents_plus_fable():
    planner, line = _start()
    assert "Fable 5.2 on" in line
    models = _plan(planner, True, "62")
    assert models[:3] == [AGENT, AGENT2, FABLE] and len(models) == 5 and all("flash" in m for m in models[3:])
    assert planner.claude.caps["62"] == pytest.approx(frontier.AGENT2_SEASONAL_QUESTION_CAP + 0.72)
    assert planner.last_plan["fable"] is True
    # The day cap is unchanged; the MiniBench limit leaves room for agent + agent2 + Fable
    # ($2.88 + $1.64 + $0.72 = $5.24 -> $5.30): $20 - $5.30 = $14.70 (architect, 12 Nov; was $15.40).
    assert planner.claude.daily_cap == 20.00 and planner.claude.minibench_daily_cap == 14.70
    assert frontier.seasonal_reserve(2, fable=True) == 5.30 and frontier.seasonal_reserve(2) == 4.60


def test_seasonal_one_agent_plus_fable():
    planner, _ = _start(agents=1)
    assert _plan(planner, True, "63")[:3] == [AGENT, OPUS_55, FABLE]
    assert planner.claude.caps["63"] == pytest.approx(frontier.AGENT_SEASONAL_QUESTION_CAP + 0.72)


def test_minibench_agent_opus_and_fable():
    planner, _ = _start()
    models = _plan(planner, False, "64")
    assert models[:3] == [AGENT, OPUS_55, FABLE] and len(models) == 5
    assert planner.claude.caps["64"] == pytest.approx(frontier.AGENT_MINIBENCH_QUESTION_CAP + 0.72)


def test_minibench_burst_day_fable_follows_plain_opus():
    planner, _ = _start(_budget(day=11.0))  # whole day past $10: MiniBench drops the agent
    models = _plan(planner, False, "65")
    assert models[:2] == [OPUS_55, FABLE] and AGENT not in models
    assert planner.claude.caps["65"] == pytest.approx(frontier.CLAUDE_MINIBENCH_QUESTION_CAP + 0.72)


@pytest.mark.parametrize("day, claude", [(13.60, True), (14.40, False), (15.20, False)])
def test_minibench_day_limit_14_70_with_fable(day, claude):
    # MiniBench limit $14.70: 13.60 + Opus $0.36 + Fable $0.72 = $14.68 fits; 14.40 + 0.36 does not.
    planner, _ = _start(_budget(day=day))
    models = _plan(planner, False, "72")
    assert (OPUS_55 in models and FABLE in models) is claude


def test_one_agent_with_fable_minibench_limit_11_00():
    planner, _ = _start(agents=1)
    assert planner.claude.daily_cap == 15.00 and planner.claude.minibench_daily_cap == 11.00


def test_low_mode_keeps_the_limit_without_fable():
    claude = _budget()
    claude.cycles[claude.cycle.isoformat()] = 170 - 10.0
    planner, _ = _start(claude)
    assert planner.mode == "low" and planner.claude.minibench_daily_cap == 15.40


def test_minibench_over_its_day_limit_no_opus_no_fable():
    planner, _ = _start(_budget(day=15.20))  # MiniBench limit $14.70: plain Opus ($0.36) refused
    models = _plan(planner, False, "66")
    assert OPUS_55 not in models and FABLE not in models


def test_fable_refused_by_the_day_cap_is_skipped_and_the_cap_kept():
    planner, _ = _start(_budget(day=19.50))  # $20 day: Opus $0.36 fits, Fable $0.72 does not
    models = _plan(planner, True, "67")
    assert FABLE not in models and OPUS_55 in models
    assert planner.claude.caps["67"] == frontier.CLAUDE_SEASONAL_QUESTION_CAP


@pytest.mark.parametrize("spent, mode", [(170 - 10.0, "low"), (170 - 4.0, "minimal")])
def test_no_fable_in_low_or_minimal_mode(spent, mode):
    claude = _budget()
    claude.cycles[claude.cycle.isoformat()] = spent
    planner, line = _start(claude)
    assert planner.mode == mode and f"Fable 5.2 off ({mode} mode)" in line
    assert FABLE not in _plan(planner, True, "68") and FABLE not in _plan(planner, False, "69")


def test_switched_off_no_fable(monkeypatch):
    monkeypatch.setattr(bot_config, "FRONTIER_FABLE_ENABLED", False)
    planner, line = _start()
    assert "Fable" not in line and FABLE not in _plan(planner, True, "70")


def test_plain_lineup_without_agents_gets_fable():
    planner, _ = _start(agent=False)
    assert _plan(planner, True, "71")[:3] == [OPUS_55, OPUS_55, FABLE]
    assert planner.claude.caps["71"] == pytest.approx(frontier.CLAUDE_SEASONAL_QUESTION_CAP + 0.72)


def test_fable_counts_as_a_claude_forecast():
    assert aggregation.is_claude_forecast(FABLE) and aggregation.is_claude_forecast("replay/" + FABLE)
    assert not aggregation.is_claude_forecast("anthropic/claude-haiku-5-5")
    # claude_first: Fable + one agent = 2 Claude forecasts, so Flash is left out.
    chosen, used = aggregation.choose([AGENT, FABLE, "gemini/gemini-3.6-flash", "gemini/gemini-3.8-flash"], "claude_first")
    assert chosen == [0, 1] and used == "claude"
