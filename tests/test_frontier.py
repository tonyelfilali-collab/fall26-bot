"""Frontier lineup (frontier.py): billing cycle, step-downs, caps, credit error, the Claude call, the plan."""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

import bot_config
import frontier
from frontier import GPT_SOL, HAIKU, OPUS_5, OPUS_55, ClaudeBudget
from gemini_budget import MemoryStore, current_question_key
from llm_throttle import NoAnswer
from spend import SpendGuard, question_seasonal, utc_day

RESET = date(2026, 10, 14)


def _budget(spent: float = 0.0, data: dict | None = None, today: date = date(2026, 10, 21)) -> ClaudeBudget:
    claude = ClaudeBudget(MemoryStore(data if data is not None else {"days": {}}), 170.0, RESET, today=today)
    if spent:
        claude.cycles[claude.cycle.isoformat()] = spent
    return claude


# ---------------------------------------------------------------- cycle and step-downs


@pytest.mark.parametrize("today, start", [
    (date(2026, 10, 21), date(2026, 10, 14)), (date(2026, 10, 14), date(2026, 10, 14)),
    (date(2026, 10, 13), date(2026, 9, 14)), (date(2027, 1, 2), date(2026, 12, 14)),
])
def test_cycle_start(today, start):
    assert frontier.cycle_start(today, RESET) == start


def test_this_cycle_ends_17_nov():
    reset = frontier.reset_date(bot_config.CLAUDE_CYCLE_RESET_DATE)
    assert frontier.cycle_start(date(2026, 10, 21), reset) == date(2026, 10, 18)
    assert frontier.cycle_start(date(2026, 11, 17), reset) == date(2026, 10, 18)
    assert frontier.cycle_start(date(2026, 11, 18), reset) == date(2026, 11, 18)


def test_cycle_start_in_short_months():
    assert frontier.cycle_start(date(2027, 2, 28), date(2026, 10, 31)) == date(2027, 2, 28)
    assert frontier.cycle_start(date(2027, 2, 27), date(2026, 10, 31)) == date(2027, 1, 31)


@pytest.mark.parametrize("spent, mode", [(0, "full"), (127.4, "full"), (128, "low"), (152.9, "low"), (153.1, "minimal"), (170, "off")])
def test_step_downs(spent, mode):
    assert frontier.claude_mode(_budget(spent))[0] == mode


def test_fail_closed_modes():
    assert frontier.claude_mode(_budget(), api_key=False)[0] == "off"
    assert frontier.claude_mode(ClaudeBudget(MemoryStore(None), 170.0, RESET))[0] == "off"  # missing ledger
    assert frontier.claude_mode(ClaudeBudget(MemoryStore({"days": {}}), 170.0, None))[0] == "off"  # no reset date
    claude = _budget()
    claude.mark_credit_error()
    assert frontier.claude_mode(claude)[0] == "off" and claude.blocked


def test_credit_error_is_recorded_and_ends_with_the_cycle():
    claude = _budget()
    error = RuntimeError("Your credit balance is too low to access the Anthropic API.")
    error.status_code = 400  # type: ignore[attr-defined]
    token = current_question_key.set("1")
    try:
        claude.finish(OPUS_55, False, error=error)
    finally:
        current_question_key.reset(token)
    assert claude.blocked and claude.question_spent("1") == 0.0  # a 400: never billed
    saved = claude.snapshot()
    assert saved["credit_errors"] == {"2026-10-14": utc_day()}
    later = _budget(data=saved, today=date(2026, 10, 30))
    assert frontier.claude_mode(later)[0] == "off"
    next_cycle = _budget(data=saved, today=date(2026, 11, 14))
    assert frontier.claude_mode(next_cycle)[0] == "full"


def test_cycle_spend_is_kept_and_saved():
    claude = _budget()
    token = current_question_key.set("7")
    try:
        claude.start(OPUS_55)
        claude.finish(OPUS_55, True, cost=0.21)
        claude.start(OPUS_5)
        claude.finish(OPUS_5, False, error=_status_error(529))  # overloaded: not billed
    finally:
        current_question_key.reset(token)
    assert claude.cycle_spent() == pytest.approx(0.21)
    assert ClaudeBudget(MemoryStore(claude.snapshot()), 170.0, RESET, today=date(2026, 10, 21)).cycle_spent() == pytest.approx(0.21)


def _status_error(status: int) -> Exception:
    error = RuntimeError(f"HTTP {status}")
    error.status_code = status  # type: ignore[attr-defined]
    return error


def test_a_refusal_is_charged_its_real_cost():
    claude = _budget()
    token = current_question_key.set("8")
    try:
        claude.start(HAIKU)
        claude.finish(HAIKU, False, error=NoAnswer("refused", cost=0.02))
    finally:
        current_question_key.reset(token)
    assert claude.question_spent("8") == pytest.approx(0.02)


def test_worst_case_reservations_fit_the_question_caps():
    assert 2 * frontier.claude_reserve(OPUS_55) <= frontier.CLAUDE_SEASONAL_QUESTION_CAP
    assert frontier.claude_reserve(OPUS_55) <= frontier.CLAUDE_MINIBENCH_QUESTION_CAP
    assert set(frontier.CLAUDE_PRICES) == {OPUS_55, OPUS_5, HAIKU}


def test_claude_caps_refuse_before_passing():
    claude = _budget()
    claude.days[utc_day()] = 14.70
    token, seasonal = current_question_key.set("9"), question_seasonal.set(True)
    try:
        assert claude.refusal(OPUS_55) == "daily cap"  # $0.36 worst case would pass $15
        assert claude.refusal(HAIKU) is None
        claude.days[utc_day()] = 0.0
        claude.set_cap("9", 0.60)
        claude.questions["9"] = {"spent": 0.30}
        assert claude.refusal(OPUS_55) == "cap"
    finally:
        current_question_key.reset(token), question_seasonal.reset(seasonal)


@pytest.mark.parametrize("flag", [False, None])  # MiniBench, or an unknown tournament (fail closed)
def test_a_minibench_burst_leaves_seasonal_its_claude(flag):
    claude = _budget()
    for q in range(40):
        tokens = current_question_key.set(f"m{q}"), question_seasonal.set(flag)
        try:
            while claude.refusal(OPUS_55) is None:
                claude.start(OPUS_55)
                assert claude.day_spent() + sum(claude.in_flight.values()) <= frontier.CLAUDE_MINIBENCH_DAY_LIMIT + 1e-9
                claude.finish(OPUS_55, True, cost=frontier.claude_reserve(OPUS_55))
            assert claude.refusal(OPUS_55) == "daily cap"
        finally:
            current_question_key.reset(tokens[0]), question_seasonal.reset(tokens[1])
    assert frontier.CLAUDE_MINIBENCH_DAY_LIMIT - 0.36 < claude.day_spent() <= frontier.CLAUDE_MINIBENCH_DAY_LIMIT + 1e-9
    tokens = current_question_key.set("s1"), question_seasonal.set(True)
    try:
        for _ in range(2):  # the seasonal question's two Opus slots, both running at once
            assert claude.refusal(OPUS_55) is None
            claude.start(OPUS_55)
    finally:
        current_question_key.reset(tokens[0]), question_seasonal.reset(tokens[1])
    assert frontier.CLAUDE_DAILY_CAP - frontier.CLAUDE_MINIBENCH_DAY_LIMIT >= 6.0


# ---------------------------------------------------------------- the Claude call


class _FakeMessages:
    def __init__(self, response, seen):
        self.response, self.seen = response, seen

    async def create(self, **kwargs):
        self.seen.update(kwargs)
        return self.response


def _fake_client(response, seen):
    class Client:
        def __init__(self, **kwargs):
            seen["client"] = kwargs
            self.messages = _FakeMessages(response, seen)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    return Client


def _response(text="Probability: 40%", stop="end_turn", out=1200):
    return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
                           stop_reason=stop, usage=SimpleNamespace(input_tokens=7000, output_tokens=out,
                                                                   cache_creation_input_tokens=0, cache_read_input_tokens=0))


def test_claude_call_request_and_cost(monkeypatch):
    import anthropic

    seen: dict = {}
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _fake_client(_response(), seen))
    llm = frontier._ClaudeCall(model=OPUS_55, temperature=None, timeout=60, allowed_tries=1)
    reply = asyncio.run(llm._mockable_direct_call_to_model("Forecast this."))
    assert seen["model"] == "claude-opus-5-5" and seen["output_config"] == {"effort": "high"}
    assert "temperature" not in seen and "thinking" not in seen and seen["max_tokens"] == frontier.CLAUDE_MAX_TOKENS
    assert seen["client"]["max_retries"] == 0  # failures go to the chain's backup, never an SDK retry
    assert reply.data == "Probability: 40%" and reply.completion_tokens_used == 1200
    assert reply.cost == pytest.approx((7000 * 4 + 1200 * 20) / 1e6)


@pytest.mark.parametrize("stop, text", [("refusal", ""), ("max_tokens", "Half an"), ("end_turn", "")])
def test_claude_no_usable_answer_goes_to_the_backup(monkeypatch, stop, text):
    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _fake_client(_response(text=text, stop=stop), {}))
    llm = frontier._ClaudeCall(model=OPUS_5, temperature=None, timeout=60, allowed_tries=1)
    with pytest.raises(NoAnswer) as caught:
        asyncio.run(llm._mockable_direct_call_to_model("Forecast this."))
    assert caught.value.cost and caught.value.cost > 0


def test_system_messages_go_to_the_system_field():
    system, messages = frontier._messages([{"role": "system", "content": "S"}, {"role": "user", "content": "U"}])
    assert system == "S" and messages == [{"role": "user", "content": "U"}]


# ---------------------------------------------------------------- the plan


def _planner(claude: ClaudeBudget, key_left: float = 5.0):
    spend = SpendGuard(MemoryStore({"days": {}}))
    planner = frontier._frontier_pool(MemoryStore(), spend, claude)
    planner.agent_enabled = False  # these tests cover the plain lineup (the agent: test_frontier_agent.py)
    planner.start_run(key_left, api_key=True)
    return planner


def _models(chains) -> list[str]:
    return [c.model for c in chains]


def test_full_plan_seasonal_and_minibench():
    planner = _planner(_budget())
    token = current_question_key.set("1")
    try:
        seasonal = planner.plan(True, binary=False)
        assert _models(seasonal)[:2] == [OPUS_55, OPUS_55]
        assert len(seasonal) == 4 and all("flash" in m for m in _models(seasonal)[2:])
        assert planner.spend.forecast_target is None  # no top-ups: Flash never outvotes Claude
        assert planner.claude.caps["1"] == frontier.CLAUDE_SEASONAL_QUESTION_CAP
        assert planner.round2(True, []) == []
    finally:
        current_question_key.reset(token)
    token = current_question_key.set("2")
    try:
        minibench = planner.plan(False)
        assert _models(minibench)[0] == OPUS_55 and len(minibench) == 3
        assert planner.claude.caps["2"] == frontier.CLAUDE_MINIBENCH_QUESTION_CAP
        assert planner.spend.caps["2"] == bot_config.PAID_FLASH_QUESTION_CAP  # MiniBench Flash: unchanged caps
    finally:
        current_question_key.reset(token)


@pytest.mark.parametrize("spent, seasonal_opus, minibench_opus", [(0.80 * 170, 1, 1), (0.95 * 170, 1, 0)])
def test_step_down_plans(spent, seasonal_opus, minibench_opus):
    planner = _planner(_budget(spent))
    for key, seasonal, opus in (("5", True, seasonal_opus), ("6", False, minibench_opus)):
        token = current_question_key.set(key)
        try:
            assert _models(planner.plan(seasonal)).count(OPUS_55) == opus
        finally:
            current_question_key.reset(token)


def test_backup_chains():
    planner = _planner(_budget())
    opus = planner._claude_chain(OPUS_55)
    assert opus._backup.model == OPUS_5 and opus._backup._backup.model == HAIKU and opus._backup._backup._backup is None
    gpt = planner._gpt_chain()
    assert gpt._backup.model.startswith("gemini/") and gpt._backup_on_any_error and gpt.allowed_tries == 1


def test_gpt_is_off_by_default_and_needs_the_key_left():
    planner = _planner(_budget())
    token = current_question_key.set("3")
    try:
        assert GPT_SOL not in _models(planner.plan(True))
        planner.gpt_enabled = True
        planner.start_run(5.0, api_key=True)
        assert _models(planner.plan(True))[:3] == [OPUS_55, OPUS_55, GPT_SOL]
        assert planner.spend.caps["3"] == frontier.OPENROUTER_SEASONAL_QUESTION_CAP
        planner.start_run(1.99, api_key=True)
        assert GPT_SOL not in _models(planner.plan(True))
    finally:
        current_question_key.reset(token)


def test_off_is_todays_setup():
    off = _planner(ClaudeBudget(MemoryStore(None), 170.0, RESET))
    token = current_question_key.set("4")
    try:
        assert all(m.startswith("gemini/") for m in _models(off.plan(True)))
        assert off.spend.forecast_target == 3
    finally:
        current_question_key.reset(token)


def test_frontier_is_built_off():
    assert bot_config.FRONTIER_ENABLED is True  # PR 3: switched on
    assert bot_config.CLAUDE_BUDGET_PER_CYCLE == 170.0
    assert bot_config.FRONTIER_GPT_ENABLED is False
    assert bot_config.get_lineup("frontier").test_only


def test_rehearsal_passes():
    import frontier_rehearsal

    ok, text = frontier_rehearsal.report()
    assert ok, text


def test_each_claude_call_records_input_tokens_and_stop_reason():
    claude = _budget()
    token = current_question_key.set("31")
    try:
        claude.start(OPUS_55)
        claude.finish(OPUS_55, True, cost=0.2, output_tokens=900, seconds=400.0, input_tokens=7000, stop_reason="end_turn")
        claude.start(OPUS_55)
        claude.finish(OPUS_55, False, error=NoAnswer("cut off", cost=0.3, stop_reason="max_tokens"),
                      stop_reason="max_tokens")
    finally:
        current_question_key.reset(token)
    calls = claude.questions["31"]["calls"]
    assert calls[0]["input_tokens"] == 7000 and calls[0]["stop_reason"] == "end_turn"
    assert calls[1]["stop_reason"] == "max_tokens" and calls[1]["charged"] == pytest.approx(0.3)
    planner = _planner(claude)
    planner.calls_before = {}
    report = frontier.frontier_report(planner)
    assert "| 31 | claude-opus-5-5 | 400 **over 6 min** | 7000 | 900 | $0.2000 | end_turn |" in report
    assert "| max_tokens |" in report
