"""Frontier agent (frontier.py, PR 4, built OFF): Opus 5.5 + web_search / web_fetch with fake SDK replies (0 real calls)."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from types import SimpleNamespace

import pytest

import bot_config
import frontier
from frontier import AGENT, HAIKU, OPUS_5, OPUS_55, ClaudeBudget
from gemini_budget import MemoryStore, current_question_key
from spend import SpendCapReached, SpendGuard, question_seasonal, utc_day

RESET = date(2026, 10, 14)
URL_READ = "https://example.org/secret-page"
URL_FOUND = "https://example.org/found"


def _budget(day: float = 0.0) -> ClaudeBudget:
    claude = ClaudeBudget(MemoryStore({"days": {}}), 170.0, RESET, today=date(2026, 10, 21))
    if day:
        claude.days[utc_day()] = day
    return claude


def _usage(inp=8000, out=1500, searches=0, fetches=0, server=True):
    tools = SimpleNamespace(web_search_requests=searches, web_fetch_requests=fetches) if server else None
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=0,
                           cache_read_input_tokens=0, server_tool_use=tools)


def _text(text):
    return SimpleNamespace(type="text", text=text)


def _search(*urls):
    return [SimpleNamespace(type="server_tool_use", name="web_search", id="s", input={"query": "q"}),
            SimpleNamespace(type="web_search_tool_result", tool_use_id="s",
                            content=[SimpleNamespace(type="web_search_result", url=u, title="t") for u in urls])]


def _fetch(url):
    return [SimpleNamespace(type="server_tool_use", name="web_fetch", id="f", input={"url": url}),
            SimpleNamespace(type="web_fetch_tool_result", tool_use_id="f",
                            content=SimpleNamespace(type="web_fetch_result", url=url, content={"type": "document"}))]


def _tool_error(tool, code):
    return SimpleNamespace(type=f"{tool}_tool_result", tool_use_id="e",
                           content=SimpleNamespace(type=f"{tool}_tool_result_error", error_code=code))


def _reply(content, stop="end_turn", **usage):
    return SimpleNamespace(content=content, stop_reason=stop, usage=_usage(**usage))


def _plain(text="Probability: 30%"):
    return SimpleNamespace(content=[_text(text)], stop_reason="end_turn",
                           usage=SimpleNamespace(input_tokens=7000, output_tokens=1000, cache_creation_input_tokens=0,
                                                 cache_read_input_tokens=0))


class Fake:
    """anthropic.AsyncAnthropic stand-in: agent requests (with tools) get the
    scripted replies in order; plain requests the plain reply."""

    def __init__(self, agent_replies, plain=None, agent_delay=0.0):
        self.agent_replies, self.plain = list(agent_replies), plain or _plain()
        self.agent_delay, self.requests, self.clients = agent_delay, [], []

    def __call__(self, **kwargs):
        fake = self
        fake.clients.append(kwargs)

        class Messages:
            async def create(self, **request):
                fake.requests.append(request)
                if "tools" not in request:
                    if isinstance(fake.plain, BaseException):
                        raise fake.plain
                    return fake.plain
                if fake.agent_delay:
                    await asyncio.sleep(fake.agent_delay)
                item = fake.agent_replies.pop(0)
                if isinstance(item, BaseException):
                    raise item
                return item

        class Client:
            messages = Messages()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        return Client()

    def agent_requests(self):
        return [r for r in self.requests if "tools" in r]

    def plain_requests(self):
        return [r for r in self.requests if "tools" not in r]


def _planner(claude: ClaudeBudget, agent: bool = True):
    planner = frontier._frontier_pool(MemoryStore(), SpendGuard(MemoryStore({"days": {}})), claude)
    planner.agent_enabled = agent
    planner.claude_pacer = frontier.RequestPacer(60000)
    planner.start_run(5.0, api_key=True)
    return planner


def _run(monkeypatch, fake, claude=None, seasonal=True, question="11", deadline=None):
    """The agent chain (agent -> plain Opus chain) on one prompt; returns (reply or error, budget, agent log)."""
    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", fake)
    claude = claude or _budget()
    planner = _planner(claude)

    async def go():
        current_question_key.set(question)
        question_seasonal.set(seasonal)
        claude.set_cap(question, frontier.AGENT_SEASONAL_QUESTION_CAP if seasonal else frontier.AGENT_MINIBENCH_QUESTION_CAP)
        frontier.forecast_deadline.set(None if deadline is None else time.monotonic() + deadline)
        log = []
        frontier.agent_log.set(log)
        try:
            reply = await planner._agent_chain()._mockable_direct_call_to_model("Forecast this. Probability: ZZ%")
        except Exception as e:  # noqa: BLE001
            reply = e
        return reply, log

    reply, log = asyncio.run(go())
    return reply, claude, log


def _calls(claude, question="11"):
    return claude.questions.get(question, {}).get("calls", [])


# ---------------------------------------------------------------- the agent call


def test_request_shape_tools_and_answer(monkeypatch):
    fake = Fake([_reply([*_search(URL_FOUND), *_fetch(URL_READ), _text("Probability: 62%")], searches=1, fetches=1)])
    reply, claude, log = _run(monkeypatch, fake)
    assert reply.data == "Probability: 62%"
    request = fake.agent_requests()[0]
    assert request["model"] == "claude-opus-5-5" and request["output_config"] == {"effort": "high"}
    assert "thinking" not in request and "temperature" not in request
    assert request["tools"] == [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 8},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5, "max_content_tokens": 5000},
    ]
    assert request["messages"] == [{"role": "user", "content": "Forecast this. Probability: ZZ%"}]  # the same prompt
    assert "8 searches" in request["system"] and "5 pages" in request["system"]
    assert fake.clients[0]["max_retries"] == 0
    assert not fake.plain_requests()  # no fallback


def test_minibench_caps(monkeypatch):
    fake = Fake([_reply([_text("Probability: 50%")])])
    _run(monkeypatch, fake, seasonal=False)
    tools = fake.agent_requests()[0]["tools"]
    assert tools[0]["max_uses"] == 4 and tools[1]["max_uses"] == 3
    assert fake.agent_requests()[0]["max_tokens"] == 12000


def test_pause_turn_is_continued_with_the_same_content(monkeypatch):
    first = [_text("Checking. "), *_search(URL_FOUND, URL_FOUND + "2")]
    fake = Fake([_reply(first, stop="pause_turn", searches=3, fetches=1, out=4000),
                 _reply([_text("Probability: 41%")], searches=1, out=2000)])
    reply, claude, log = _run(monkeypatch, fake)
    assert reply.data == "Checking. Probability: 41%"
    second = fake.agent_requests()[1]
    assert second["messages"][0]["role"] == "user" and second["messages"][1] == {"role": "assistant", "content": first}
    assert len(second["messages"]) == 2  # no "continue" user message
    assert second["tools"][0]["max_uses"] == 5 and second["tools"][1]["max_uses"] == 4  # what is left
    assert second["max_tokens"] == 16000  # min(16k, 20k - 4k)
    assert log[0]["requests"] == 2 and log[0]["searches"] == 4 and log[0]["fetches"] == 1


def test_two_pauses_extend_one_assistant_message(monkeypatch):
    fake = Fake([_reply([_text("a")], stop="pause_turn", searches=1), _reply([_text("b")], stop="pause_turn", searches=1),
                 _reply([_text("Probability: 5%")])])
    reply, _, _ = _run(monkeypatch, fake)
    third = fake.agent_requests()[2]["messages"]
    assert len(third) == 2 and [b.text for b in third[1]["content"]] == ["a", "b"]
    assert reply.data == "abProbability: 5%"


def test_cost_counts_tokens_and_one_cent_per_search(monkeypatch):
    fake = Fake([_reply([_text("Probability: 10%")], inp=50000, out=6000, searches=6, fetches=2)])
    reply, claude, log = _run(monkeypatch, fake)
    expected = (50000 * 4 + 6000 * 20) / 1e6 + 6 * 0.01
    assert reply.cost == pytest.approx(expected) and claude.question_spent("11") == pytest.approx(expected)
    call = _calls(claude)[-1]
    assert call["model"] == AGENT and call["ok"] and call["searches"] == 6 and call["fetches"] == 2
    assert log[0]["cost"] == pytest.approx(expected)


def test_counts_from_blocks_when_usage_has_none(monkeypatch):
    reply = _reply([*_search(URL_FOUND), *_search(URL_FOUND), *_fetch(URL_READ), _text("Probability: 1%")])
    reply.usage = _usage(server=False)
    _, claude, log = _run(monkeypatch, Fake([reply]))
    assert log[0]["searches"] == 2 and log[0]["fetches"] == 1


def test_tool_errors_are_counted_and_the_answer_kept(monkeypatch):
    content = [_tool_error("web_fetch", "url_not_accessible"), _tool_error("web_search", "max_uses_exceeded"),
               _tool_error("web_fetch", "url_not_accessible"), _text("Probability: 33%")]
    reply, _, log = _run(monkeypatch, Fake([_reply(content, searches=0, fetches=2)]))
    assert reply.data == "Probability: 33%"
    assert log[0]["tool_errors"] == {"web_fetch:url_not_accessible": 2, "web_search:max_uses_exceeded": 1}
    assert log[0]["urls_read"] == []


def test_a_request_error_goes_to_plain_opus(monkeypatch):
    import anthropic
    import httpx2 as httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.BadRequestError("web search is not enabled", response=httpx.Response(400, request=request), body=None)
    reply, claude, log = _run(monkeypatch, Fake([error]))
    assert reply.data == "Probability: 30%"  # the plain Opus fallback
    calls = _calls(claude)
    assert [c["model"] for c in calls] == [AGENT, OPUS_55] and calls[0]["charged"] == 0.0  # a 400: never billed
    assert log[0]["outcome"] == "failed"


# ---------------------------------------------------------------- time cap


def test_timeout_cancels_and_falls_back_to_plain_opus(monkeypatch):
    monkeypatch.setattr(frontier, "AGENT_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(frontier, "AGENT_MIN_SECONDS", 0.0)
    fake = Fake([_reply([_text("Probability: 99%")])], agent_delay=5.0)
    started = time.monotonic()
    reply, claude, log = _run(monkeypatch, fake)
    assert time.monotonic() - started < 3  # cancelled, not waited for
    assert reply.data == "Probability: 30%"
    agent, plain = _calls(claude)
    assert not agent["ok"] and agent["charged"] == pytest.approx(frontier.agent_reserve(True))  # its worst case
    assert plain["model"] == OPUS_55 and plain["ok"]
    assert log[0]["outcome"] == "timed out" and log[0]["cost"] == pytest.approx(frontier.agent_reserve(True))


def test_fallback_gets_only_the_time_left(monkeypatch):
    # 130 s left in the planned wait: no agent (it would leave the fallback < 120 s);
    # the plain Opus fallback gets 130 - 20 = 110 s, not 6 minutes.
    fake = Fake([])
    reply, claude, log = _run(monkeypatch, fake, deadline=130)
    assert reply.data == "Probability: 30%" and not fake.agent_requests()
    assert 100 < fake.clients[-1]["timeout"] <= 110
    assert _calls(claude)[0]["charged"] == 0.0


def test_agent_time_cap_keeps_the_fallback_time(monkeypatch):
    fake = Fake([_reply([_text("Probability: 20%")])])
    _run(monkeypatch, fake, deadline=400)
    assert 270 < fake.clients[0]["timeout"] <= 280  # 400 - 120 s kept for the fallback
    fake = Fake([_reply([_text("Probability: 20%")])])
    _run(monkeypatch, fake, deadline=900)
    assert fake.clients[0]["timeout"] == frontier.AGENT_TIMEOUT_SECONDS  # 7 minutes


def test_no_time_for_the_fallback_either(monkeypatch):
    reply, claude, _ = _run(monkeypatch, Fake([]), deadline=50)
    assert not isinstance(reply, SimpleNamespace) and claude.question_spent("11") == 0.0  # nothing started


def test_plain_opus_slots_keep_the_full_timeout(monkeypatch):
    import anthropic

    fake = Fake([])
    monkeypatch.setattr(anthropic, "AsyncAnthropic", fake)
    llm = frontier._ClaudeCall(model=OPUS_55, temperature=None, timeout=60, allowed_tries=1)

    async def go():
        frontier.forecast_deadline.set(time.monotonic() + 100)
        return await llm._mockable_direct_call_to_model("x")

    asyncio.run(go())
    assert fake.clients[0]["timeout"] == frontier.CLAUDE_TIMEOUT_SECONDS  # not deadline-aware: unchanged


# ---------------------------------------------------------------- caps and money


def test_worst_case_reservations_fit_the_raised_caps():
    plain = frontier.claude_reserve(OPUS_55)
    # Seasonal: agent + the plain Opus slot + the agent's Opus fallback.
    assert frontier.agent_reserve(True) + 2 * plain <= frontier.AGENT_SEASONAL_QUESTION_CAP + 1e-9
    # MiniBench: agent + its Opus fallback.
    assert frontier.agent_reserve(False) + plain <= frontier.AGENT_MINIBENCH_QUESTION_CAP + 1e-9
    assert frontier.agent_reserve(True) == pytest.approx(1.28) and frontier.agent_reserve(False) == pytest.approx(0.64)


def test_reservation_follows_the_question_kind():
    claude = _budget()

    async def go(seasonal):
        question_seasonal.set(seasonal)
        return claude.estimate(AGENT)

    assert asyncio.run(go(True)) == pytest.approx(1.28)
    assert asyncio.run(go(False)) == pytest.approx(0.64)
    assert asyncio.run(go(None)) == pytest.approx(1.28)  # unknown: the larger one (fail closed)


def test_plan_raises_the_caps_and_replaces_one_opus_slot():
    planner = _planner(_budget())
    token = current_question_key.set("21")
    try:
        chains = planner.plan(True)
        assert [c.model for c in chains][:2] == [AGENT, OPUS_55] and len(chains) == 4
        assert planner.claude.caps["21"] == 2.00 and planner.last_plan["agent"] is True
        assert chains[0]._backup.model == OPUS_55 and chains[0]._backup.deadline_aware
        assert chains[0]._backup_on_any_error and not chains[1].deadline_aware
    finally:
        current_question_key.reset(token)
    token = current_question_key.set("22")
    try:
        chains = planner.plan(False)
        assert [c.model for c in chains][0] == AGENT and len(chains) == 3
        assert planner.claude.caps["22"] == 1.00
    finally:
        current_question_key.reset(token)


def test_daily_cap_refuses_the_agent_and_keeps_plain_opus():
    planner = _planner(_budget(day=14.00))  # $1.00 left today: the agent's $1.28 does not fit
    token, seasonal = current_question_key.set("23"), question_seasonal.set(True)
    try:
        models = [c.model for c in planner.plan(True)]
        assert AGENT not in models and models[:2] == [OPUS_55, OPUS_55]
        assert planner.claude.caps["23"] == frontier.CLAUDE_SEASONAL_QUESTION_CAP
    finally:
        current_question_key.reset(token), question_seasonal.reset(seasonal)


def test_a_cap_refused_agent_hands_over_to_plain_opus(monkeypatch):
    claude = _budget()
    claude.questions["11"] = {"spent": 1.00}  # $1.00 left of $2.00: the agent ($1.28) would pass it
    reply, claude, _ = _run(monkeypatch, Fake([]), claude=claude)
    assert reply.data == "Probability: 30%" and [c["model"] for c in _calls(claude)] == [OPUS_55]
    assert claude.question_spent("11") <= frontier.AGENT_SEASONAL_QUESTION_CAP


@pytest.mark.parametrize("spent, mode", [(0.80 * 170, "low"), (0.95 * 170, "minimal")])
def test_step_downs_use_plain_opus(spent, mode):
    claude = _budget()
    claude.cycles[claude.cycle.isoformat()] = spent
    planner = _planner(claude)
    assert planner.mode == mode and "agent off (" in planner.start_run(5.0, api_key=True)
    token = current_question_key.set("24")
    try:
        assert AGENT not in [c.model for c in planner.plan(True)]
        assert planner.claude.caps["24"] == frontier.CLAUDE_SEASONAL_QUESTION_CAP
    finally:
        current_question_key.reset(token)


def test_paused_with_tool_caps_used_up_is_not_continued(monkeypatch):
    fake = Fake([_reply([*_search(URL_FOUND)], stop="pause_turn", searches=8, fetches=2)])
    reply, claude, log = _run(monkeypatch, fake)
    assert len(fake.agent_requests()) == 1 and reply.data == "Probability: 30%"  # plain Opus
    agent = _calls(claude)[0]
    assert agent["charged"] == pytest.approx((8000 * 4 + 1500 * 20) / 1e6 + 0.08)  # its real cost
    assert log[0]["outcome"] == "paused, not continued (tool caps)"


def test_paused_over_the_reservation_is_not_continued(monkeypatch):
    fake = Fake([_reply([_text("x")], stop="pause_turn", inp=150000, out=3000, searches=2)])
    _, claude, log = _run(monkeypatch, fake)
    assert len(fake.agent_requests()) == 1 and log[0]["outcome"] == "paused, not continued (reservation)"


def test_continuations_are_capped(monkeypatch):
    fake = Fake([_reply([_text("x")], stop="pause_turn", inp=1000, out=500) for _ in range(6)])
    _, _, log = _run(monkeypatch, fake)
    assert len(fake.agent_requests()) == frontier.AGENT_MAX_CONTINUATIONS + 1
    assert log[0]["outcome"] == "paused, not continued (continuations)"


def test_a_failed_continuation_is_charged_what_was_billed(monkeypatch):
    import anthropic
    import httpx2 as httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    bad = anthropic.BadRequestError("bad", response=httpx.Response(400, request=request), body=None)
    fake = Fake([_reply([_text("x")], stop="pause_turn", searches=1), bad])
    _, claude, _ = _run(monkeypatch, fake)
    assert _calls(claude)[0]["charged"] == pytest.approx((8000 * 4 + 1500 * 20) / 1e6 + 0.01)
    cut = anthropic.APIConnectionError(request=request)
    fake = Fake([_reply([_text("x")], stop="pause_turn", searches=1), cut])
    _, claude, log = _run(monkeypatch, fake)
    assert _calls(claude)[0]["charged"] == pytest.approx(frontier.agent_reserve(True))  # cut: the worst case
    assert log[0]["cost"] == pytest.approx(frontier.agent_reserve(True))


def test_over_the_reservation_switches_the_agent_off(monkeypatch):
    fake = Fake([_reply([_text("Probability: 12%")], inp=400000, out=2000)])  # one request past the ASSUMED ceiling
    reply, claude, _ = _run(monkeypatch, fake)
    assert reply.data == "Probability: 12%" and claude.agent_off  # charged in full, agent off for the run
    planner = _planner(claude)
    planner.claude.agent_off = "over its reservation"
    token = current_question_key.set("25")
    try:
        assert AGENT not in [c.model for c in planner.plan(True)]
    finally:
        current_question_key.reset(token)


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_no_usable_answer_goes_to_plain_opus(monkeypatch, stop):
    reply, claude, _ = _run(monkeypatch, Fake([_reply([_text("half")], stop=stop)]))
    assert reply.data == "Probability: 30%" and _calls(claude)[0]["charged"] > 0


# ---------------------------------------------------------------- credit error


class CreditError(Exception):
    status_code = 400


def test_credit_error_is_never_retried(monkeypatch):
    fake = Fake([CreditError("Your credit balance is too low to access the Anthropic API.")])
    reply, claude, _ = _run(monkeypatch, fake)
    assert isinstance(reply, SpendCapReached)  # the fallback is refused: Claude is blocked
    assert len(fake.requests) == 1 and claude.blocked and claude.question_spent("11") == 0.0
    assert claude.credit_error_this_cycle()


# ---------------------------------------------------------------- logs


def test_private_log_fields(monkeypatch):
    content = [*_search(URL_FOUND), *_fetch(URL_READ), _text("Probability: 70%")]
    _, _, log = _run(monkeypatch, Fake([_reply(content, searches=1, fetches=1)]))
    entry = log[0]
    assert entry["urls_read"] == [URL_READ] and entry["urls_found"] == [URL_FOUND]
    assert entry["searches"] == 1 and entry["fetches"] == 1 and entry["outcome"] == "answered"
    assert {"seconds", "cost", "reserve", "requests", "input_tokens", "output_tokens", "tool_errors"} <= set(entry)


def test_public_logs_stay_silent(monkeypatch, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    content = [_text("Secret reasoning. "), *_search(URL_FOUND), *_fetch(URL_READ), _text("Probability: 70%")]
    fake = Fake([_reply(content, stop="pause_turn", searches=1, fetches=1), _reply([_text("More secret.")], stop="max_tokens")])
    _run(monkeypatch, fake)
    out = capsys.readouterr()
    text = caplog.text + out.out + out.err
    for secret in (URL_READ, URL_FOUND, "example.org", "Secret", "Probability: 70", "Probability: 30"):
        assert secret not in text


def test_frontier_report_adds_agent_numbers(monkeypatch):
    fake = Fake([_reply([*_fetch(URL_READ), _text("Probability: 70%")], searches=3, fetches=1)])
    _, claude, _ = _run(monkeypatch, fake)
    planner = _planner(claude)
    planner.calls_before = {}
    report = frontier.frontier_report(planner)
    assert "| Agent calls | Answered | Searches | Fetches |" in report and "| 1 | 1 | 3 | 1 |" in report
    assert "example.org" not in report


# ---------------------------------------------------------------- built OFF


def test_agent_is_built_off():
    assert bot_config.FRONTIER_AGENT_ENABLED is False
    planner = frontier._frontier_pool(MemoryStore(), SpendGuard(MemoryStore({"days": {}})), _budget())
    line = planner.start_run(5.0, api_key=True)
    assert planner.agent_enabled is False and "agent" not in line
    token = current_question_key.set("26")
    try:
        chains = planner.plan(True)
        assert [c.model for c in chains][:2] == [OPUS_55, OPUS_55] and "agent" not in planner.last_plan
        assert planner.claude.caps["26"] == frontier.CLAUDE_SEASONAL_QUESTION_CAP
        assert all(not getattr(c, "deadline_aware", False) for c in chains)
    finally:
        current_question_key.reset(token)
    assert frontier.CLAUDE_BACKUPS[OPUS_55] == [OPUS_5, HAIKU]


def test_frontier_agent_flag_is_test_only():
    import main

    source = open(main.__file__).read()
    assert '"--frontier-agent"' in source and "--frontier-agent only runs with --mode test_questions --lineup frontier." in source
    workflow = open(".github/workflows/test_bot.yaml").read()
    assert "args=(--mode test_questions --lineup frontier --frontier-agent)" in workflow
    assert "inputs.lineup == 'frontier-agent' || inputs.lineup == 'claude-preflight') && secrets.ANTHROPIC_API_KEY" in workflow


def test_the_run_summary_has_one_row_per_agent_call():
    claude = _budget()
    claude.questions["41"] = {"spent": 0.5, "calls": [{
        "model": AGENT, "ok": True, "charged": 0.5, "seconds": 200.0, "input_tokens": 160_000, "output_tokens": 3000,
        "searches": 6, "fetches": 2, "pauses": 1, "outcome": "answered", "tool_errors": {"web_fetch:url_not_accessible": 1}}]}
    planner = _planner(claude)
    planner.calls_before = {}
    report = frontier.frontier_report(planner)
    assert "| 200.0 | 160000 **over 150k** | 3000 | $0.5000 | 6 | 2 | 1 | answered | web_fetch:url_not_accessible x1 |" in report
