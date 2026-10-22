"""
Frontier lineup rehearsal (architect, 21 Oct; PR 2, second version). The REAL
frontier planner, spend guards and bot code with recorded replies: 0 model
calls, $0, nothing submitted, every ledger in memory.

A. Full mode: seasonal (4 types) = Opus 5.5 x2 + Flash x2; MiniBench = Opus 5.5
   + Flash x2; no round 2 even when they disagree; never more than 2 Flash;
   Claude <= $1.50 / $0.60 per question.
B. Under 25% of the cycle budget left: seasonal Opus x1; MiniBench Opus x1.
C. Under 10% left: seasonal Opus x1; MiniBench no Claude (today's setup).
D. "Credit balance too low": never retried, no Claude backup tried, no more
   Claude this run; every question still forecast; the next run: today's setup.
E. GPT switched on and failing: its slot goes to the Flash chain, never retried.
F. The Claude day at $14.50: no call takes it over $15; a refused Opus slot
   passes to a backup that fits (Haiku).
G. The Claude ledger can't be read: no Claude (today's setup).
H. GPT switched on, the OpenRouter key under $2 left: no GPT.
I. Timing: Opus calls slower than the planned-forecast wait are cut; the
   question is still forecast with the Flash ones.
J. Every free Flash call answers 503: paid Flash fills both Flash slots,
   although the Opus forecasts are in.
R. A burst of 40 MiniBench questions on a Claude day at $8: MiniBench stops
   using Claude at $9 (reservations included); a seasonal question in the same
   run still gets both Opus slots; the day stays under $15.

The agent (PR 4; the real agent code, the Anthropic client swapped for
recorded replies with tool results; FRONTIER_AGENT_ENABLED is pinned off for A-J):
K. Full agent path: seasonal and MiniBench = agent + Opus 5.5 + Flash x2;
   pause_turn continued; searches / fetches / URLs in the private log;
   Claude <= $3.60 / $1.60 per question.
L. Agent past its 7-minute cap (1:100 clock): cancelled, charged its worst
   case, the plain Opus fallback answers inside the planned wait; the 4
   seasonal agents' reservations ($9.12) keep MiniBench to today's setup.
L2. Agent and plain Opus both too slow: every question still forecast (Flash).
M. Tool failures (search unavailable, page not reachable): counted, answer kept.
N. One search and one fetch left at a pause: the continuation gets max_uses 1.
N2. Every search and fetch used at a pause: not continued, plain Opus answers.
O. Credit error on the agent: never retried, no fallback, no more Claude.
P. A question already near its cap: the agent is refused; seasonal plain Opus
   (or Haiku) within $1.50; MiniBench with no Claude room: today's setup.
Q. The Claude day at $14.00: no seasonal agent ($2.88 would pass $15): plain
   Opus; MiniBench no Claude (the day is over its $9 limit).
T. Free Flash quota at 0 with Claude on: the 2 Flash slots are paid Flash
   (within $0.10 a question). T2: a paid day at $0.98: the $1.00 cap refuses
   them. T3: at $0.48: seasonal paid, MiniBench refused (under $0.50 only).
S. Two seasonal agents (FRONTIER_SEASONAL_AGENTS = 2, set for the phase only):
   seasonal = agent + agent2 + Flash x2 (caps 8/3 and 4/2), MiniBench as K;
   question cap $5.30, Claude day cap $20.
S2. Both seasonal agents past their 7-minute cap: both cancelled at their
   worst case, each one's own Opus fallback answers inside the planned wait.
U1. Pacing paced-1 (cycle spend $15 over the line): seasonal unchanged,
   MiniBench plain Opus + Flash x2. U2. paced-2 ($25 over): seasonal agent +
   plain Opus + Flash x2 (no agent2), MiniBench as U1; the day cap stays $20.

Prints a report; exits 1 if a check fails.

    poetry run python frontier_rehearsal.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

from forecasting_tools import BinaryQuestion, DiscreteQuestion, MultipleChoiceQuestion, NumericQuestion

import litellm

import bot_config
import frontier
import main
from frontier import AGENT, AGENT2, GPT_SOL, HAIKU, OPUS_5, OPUS_55, ClaudeBudget, FrontierPlanner
from gemini_budget import MemoryStore
from llm_throttle import RequestPacer
from replay import ReplayChainLlm, _RecordedAnswer
from spend import SpendGuard, utc_day

RESET = date.today().replace(day=1)  # any reset day: the cycle started on the 1st
BUDGET = 170.0


class CreditError(Exception):
    """What the Anthropic SDK raises when the credits are used up (HTTP 400)."""

    status_code = 400


class _Broken(Exception):
    pass


def _question(post: int, kind: str, minutes: int = 180) -> Any:
    now = datetime.now(timezone.utc)
    common = dict(question_text=f"Rehearsal question {post}?", id_of_post=post,
                  page_url=f"https://www.metaculus.com/questions/{post}", close_time=now + timedelta(minutes=minutes),
                  open_time=now - timedelta(hours=1))
    if kind == "binary":
        return BinaryQuestion(**common)
    if kind == "multiple_choice":
        return MultipleChoiceQuestion(options=["Red", "Green", "Blue"], **common)
    bounds = dict(unit_of_measure="units", open_lower_bound=False, zero_point=None)
    if kind == "discrete":
        return DiscreteQuestion(lower_bound=0.0, upper_bound=10.0, open_upper_bound=False, cdf_size=11, **bounds, **common)
    return NumericQuestion(lower_bound=0.0, upper_bound=1000.0, open_upper_bound=True, cdf_size=201, **bounds, **common)


def _budget(spent: float = 0.0, day: float = 0.0, store: Any = None) -> ClaudeBudget:
    claude = ClaudeBudget(store or MemoryStore({"days": {}}), BUDGET, RESET)
    claude.cycles[claude.cycle.isoformat()] = spent
    if day:
        claude.days[utc_day()] = day
    return claude


def _paced(over: float) -> ClaudeBudget:
    """A cycle that started today (UTC) with its spend `over` dollars above
    the pacing line (at most $170 / 28 today): no step-down."""
    today = datetime.now(timezone.utc).date()
    claude = ClaudeBudget(MemoryStore({"days": {}}), BUDGET, today, today=today)
    claude.cycles[claude.cycle.isoformat()] = claude.pacing()[2] + over
    return claude


def run_phase(seasonal: list, minibench: list, claude: ClaudeBudget, key_left: float = 5.0,
              fail: dict | None = None, slow: dict | None = None, gpt: bool = False,
              question_log: Any = None, free_used_up: bool = False,
              openrouter_day: float = 0.0) -> tuple[list[dict], FrontierPlanner, list[str]]:
    """One live-like run; returns (question records, planner, real model of each recorded call).
    free_used_up: every free Flash model's quota at 0; openrouter_day: paid spend already today."""
    fail, slow, calls = fail or {}, slow or {}, []
    original = _RecordedAnswer._mockable_direct_call_to_model

    async def recorded(self, prompt):  # type: ignore[no-untyped-def]
        real = self.model.removeprefix("replay/")
        calls.append(real)
        if real in slow:
            await asyncio.sleep(slow[real])
        if real in fail:
            raise fail[real]()
        return await original(self, prompt)

    _RecordedAnswer._mockable_direct_call_to_model = recorded
    try:
        spend = SpendGuard(MemoryStore({"days": {utc_day(): openrouter_day} if openrouter_day else {}}))
        planner = frontier._frontier_pool(MemoryStore(), spend, claude, llm_class=ReplayChainLlm)
        if free_used_up:
            for model in bot_config.GEMINI_FORECAST_MODELS:
                planner.ledger.mark_used_up(model)
        planner.claude_llm_class = planner.gpt_llm_class = ReplayChainLlm
        planner.model_prefix = "replay/"
        planner.gpt_enabled = gpt
        planner.pacers = {m: RequestPacer(60000) for m in planner.pacers}
        planner.claude_pacer = planner.gpt_pacer = RequestPacer(60000)
        planner.start_run(key_left, api_key=True)
        parser = planner.parser()
        bot = main.FallBot2026(
            llms={"default": planner.unplanned_forecaster(), "parser": parser, "summarizer": parser, "researcher": "replay"},
            publish_reports_to_metaculus=False, enable_summarize_research=False,
            predictions_per_research_report=3, required_successful_predictions=0,
        )
        bot.planner = planner
        bot.question_log = question_log  # None: no files (a test may pass a recorder)
        bot.keep_records = True
        bot.run_mode = "test_questions"
        queued, bot.seasonal_by_post = main.run_queue(seasonal, minibench)
        asyncio.run(bot.forecast_questions(queued, return_exceptions=True))
        return bot.__dict__.get("kept_records", []), planner, calls
    finally:
        _RecordedAnswer._mockable_direct_call_to_model = original


def _info(record: dict, planner: FrontierPlanner) -> dict:
    ok = [f for f in record.get("forecasts", []) if f.get("status") == "ok"]
    models = [str(f.get("forecast_model", "")).removeprefix("replay/") for f in ok]
    post = str(record["question"]["id_of_post"])
    return {
        "post": int(post), "type": record["question"]["question_type"], "seasonal": record.get("seasonal"),
        "forecasts": len(ok), "models": models, "round2": record.get("round2"),
        "claude": sum(m.startswith("anthropic/") for m in models), "gpt": models.count(GPT_SOL),
        "opus": models.count(OPUS_55), "flash": sum("flash" in m for m in models), "agent": models.count(AGENT), "agent2": models.count(AGENT2),
        "agent_runs": [a for f in record.get("forecasts", []) for a in f.get("agent", [])],
        "paid_flash": models.count(bot_config.PAID_FLASH_MODEL), "submitted": record.get("final_forecast") is not None,
        "claude_spent": planner.claude.question_spent(post) if planner.claude else 0.0,
        "openrouter_spent": planner.spend.question_spent(post) if planner.spend else 0.0,
        "failed": [f.get("planned_model", "").removeprefix("replay/") for f in record.get("forecasts", []) if f.get("status") == "failed"],
    }


def _short(model: str) -> str:
    return model.split("/")[-1]


def _rows(infos: list[dict]) -> list[str]:
    lines = ["| Question | Type | Tournament | Forecasts | Models | Claude $ | OpenRouter $ | Submitted |",
             "|---|---|---|---|---|---|---|---|"]
    for i in infos:
        lines.append(f"| {i['post']} | {i['type']} | {'seasonal' if i['seasonal'] else 'MiniBench'} | {i['forecasts']} | "
                     f"{', '.join(_short(m) for m in i['models'])} | ${i['claude_spent']:.3f} | ${i['openrouter_spent']:.3f} | "
                     f"{'yes' if i['submitted'] else 'no'} |")
    return lines


SEASONAL_TYPES = ("binary", "numeric", "multiple_choice", "discrete")


# ---------------------------------------------------------------- the agent (PR 4)
# The REAL agent and Claude call code (frontier.AgentLlm, frontier.ClaudeLlm)
# with the Anthropic SDK client swapped for recorded replies (tool results
# included): 0 calls. FRONTIER_AGENT_ENABLED is pinned off; the planner's switch is
# set for these phases only.

AGENT_URL = "https://example.org/rehearsal-source"


def _ns(**kw: Any) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(**kw)


def _agent_reply(content: list, stop: str = "end_turn", searches: int = 0, fetches: int = 0, inp: int = 9000, out: int = 2500) -> Any:
    usage = _ns(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=0, cache_read_input_tokens=0,
                server_tool_use=_ns(web_search_requests=searches, web_fetch_requests=fetches))
    return _ns(content=content, stop_reason=stop, usage=usage)


def _search_block() -> list:
    return [_ns(type="server_tool_use", id="s1", name="web_search", input={"query": "rehearsal"}),
            _ns(type="web_search_tool_result", tool_use_id="s1", content=[_ns(type="web_search_result", url=AGENT_URL, title="t")])]


def _fetch_block() -> list:
    return [_ns(type="server_tool_use", id="f1", name="web_fetch", input={"url": AGENT_URL}),
            _ns(type="web_fetch_tool_result", tool_use_id="f1",
                content=_ns(type="web_fetch_result", url=AGENT_URL, content={"type": "document"}))]


def _error_block(tool: str, code: str) -> Any:
    return _ns(type=f"{tool}_tool_result", tool_use_id="e1", content=_ns(type=f"{tool}_tool_result_error", error_code=code))


class RecordedAnthropic:
    """Stands in for anthropic.AsyncAnthropic. Plain requests: the recorded
    reply in the question's answer format. Agent requests (with tools): by
    `script` (full, tool-errors, near-caps, caps-used, credit), `agent_delay` / `plain_delay` seconds first."""

    def __init__(self, script: str, agent_delay: float = 0.0, plain_delay: float = 0.0) -> None:
        self.script, self.agent_delay, self.plain_delay = script, agent_delay, plain_delay
        self.agent_requests: list[dict] = []
        self.plain_requests = 0

    def __call__(self, **kwargs: Any) -> Any:
        fake = self

        class Messages:
            async def create(self, **request: Any) -> Any:
                return await fake.create(request)

        class Client:
            messages = Messages()

            async def __aenter__(self):  # type: ignore[no-untyped-def]
                return self

            async def __aexit__(self, *exc):  # type: ignore[no-untyped-def]
                return False

        return Client()

    async def create(self, request: dict) -> Any:
        from replay import current_question, recorded_reply

        question = current_question.get()
        answer = _ns(type="text", text=recorded_reply(question))
        if "tools" not in request:
            self.plain_requests += 1
            if self.plain_delay:
                await asyncio.sleep(self.plain_delay)
            usage = _ns(input_tokens=7000, output_tokens=1500, cache_creation_input_tokens=0, cache_read_input_tokens=0)
            return _ns(content=[answer], stop_reason="end_turn", usage=usage)
        self.agent_requests.append(request)
        if self.agent_delay:
            await asyncio.sleep(self.agent_delay)
        searches, fetches = request["tools"][0]["max_uses"], request["tools"][1]["max_uses"]
        first = request["messages"][-1]["role"] == "user"
        if self.script == "credit":
            raise CreditError("Your credit balance is too low to access the Anthropic API.")
        if self.script == "full":
            if first:  # pause_turn after a search and a fetch: continued as-is
                return _agent_reply([_ns(type="text", text="Checking. "), *_search_block(), *_fetch_block()], stop="pause_turn", searches=2, fetches=1)
            return _agent_reply([*_search_block(), answer], searches=1)
        if self.script == "tool-errors":
            return _agent_reply([_error_block("web_search", "unavailable"), _error_block("web_fetch", "url_not_accessible"),
                                 _error_block("web_fetch", "unsupported_content_type"), answer], fetches=2)
        if self.script == "near-caps":
            if first:  # one search and one fetch left: the continuation gets max_uses 1 / 1
                return _agent_reply([*_search_block()], stop="pause_turn", searches=searches - 1, fetches=fetches - 1)
            return _agent_reply([*_search_block(), _error_block("web_search", "max_uses_exceeded"), answer], searches=1)
        if self.script == "caps-used":  # paused with every search and fetch used: not continued
            return _agent_reply([*_search_block()], stop="pause_turn", searches=searches, fetches=fetches)
        raise ValueError(self.script)


def run_agent_phase(seasonal: list, minibench: list, claude: ClaudeBudget, fake: RecordedAnthropic,
                    seasonal_agents: int = 1) -> tuple[list[dict], FrontierPlanner, list[str]]:
    import anthropic

    original = anthropic.AsyncAnthropic
    anthropic.AsyncAnthropic = fake  # type: ignore[misc]
    try:
        spend = SpendGuard(MemoryStore({"days": {}}))
        planner = frontier._frontier_pool(MemoryStore(), spend, claude, llm_class=ReplayChainLlm)
        planner.claude_llm_class, planner.agent_llm_class = frontier.ClaudeLlm, frontier.AgentLlm
        planner.agent_enabled = True
        planner.seasonal_agents = seasonal_agents
        planner.pacers = {m: RequestPacer(60000) for m in planner.pacers}
        planner.claude_pacer = planner.gpt_pacer = RequestPacer(60000)
        planner.start_run(5.0, api_key=True)
        parser = planner.parser()
        bot = main.FallBot2026(
            llms={"default": planner.unplanned_forecaster(), "parser": parser, "summarizer": parser, "researcher": "replay"},
            publish_reports_to_metaculus=False, enable_summarize_research=False,
            predictions_per_research_report=3, required_successful_predictions=0,
        )
        bot.planner = planner
        bot.question_log = None
        bot.keep_records = True
        bot.run_mode = "test_questions"
        queued, bot.seasonal_by_post = main.run_queue(seasonal, minibench)
        asyncio.run(bot.forecast_questions(queued, return_exceptions=True))
        return bot.__dict__.get("kept_records", []), planner, []
    finally:
        anthropic.AsyncAnthropic = original  # type: ignore[misc]


AGENT_TIMES = ("AGENT_TIMEOUT_SECONDS", "AGENT_FALLBACK_SECONDS", "AGENT_MIN_SECONDS", "PARSE_MARGIN_SECONDS",
               "FALLBACK_MIN_SECONDS", "CLAUDE_TIMEOUT_SECONDS")


def run() -> dict:
    wait = main.PLANNED_WAIT_SECONDS
    main.QUICK_FORECAST_RETRY_WAIT_SECONDS, quick_wait = 0, main.QUICK_FORECAST_RETRY_WAIT_SECONDS
    # Round 1 disagrees on binary questions (Opus 70%, Flash 20%): round 2 would run on today's path.
    _RecordedAnswer.binary_percent = {OPUS_55: 70, OPUS_5: 60, HAIKU: 60, GPT_SOL: 45,
                                      **{m: 20 for m in bot_config.GEMINI_FORECAST_MODELS}}
    out: dict = {}
    times: dict = {}
    try:
        def phase(name: str, base: int, claude: ClaudeBudget, **kw: Any) -> None:
            seasonal = [_question(base + i, kind) for i, kind in enumerate(SEASONAL_TYPES)]
            records, planner, calls = run_phase(seasonal, [_question(base + 9, "binary")], claude, **kw)
            out[name] = {"infos": [_info(r, planner) for r in records], "planner": planner, "calls": calls}

        phase("A", 91000, _budget())
        phase("B", 92000, _budget(spent=0.80 * BUDGET))
        phase("C", 93000, _budget(spent=0.95 * BUDGET))
        store = MemoryStore({"days": {}})
        phase("D", 94000, _budget(store=store), fail={m: lambda: CreditError("Your credit balance is too low to access the Anthropic API.")
                                                   for m in frontier.CLAUDE_PRICES})
        out["D_next"] = frontier.claude_mode(ClaudeBudget(store, BUDGET, RESET))
        phase("E", 95000, _budget(), fail={GPT_SOL: lambda: _Broken("rehearsal: GPT down")}, gpt=True)
        phase("F", 96000, _budget(day=14.50))

        class Unreadable(MemoryStore):
            def load(self):  # type: ignore[no-untyped-def]
                raise OSError("rehearsal: ledger unreadable")

        phase("G", 97000, ClaudeBudget(Unreadable(), BUDGET, RESET))
        phase("H", 98000, _budget(), key_left=1.50, gpt=True)
        phase("T", 79000, _budget(), free_used_up=True)
        phase("T2", 78000, _budget(), free_used_up=True, openrouter_day=0.98)
        phase("T3", 77000, _budget(), free_used_up=True, openrouter_day=0.48)
        main.PLANNED_WAIT_SECONDS = 3
        phase("I", 99000, _budget(), slow={OPUS_55: 6.0})
        main.PLANNED_WAIT_SECONDS = wait
        # R: 40 MiniBench questions closing first, one seasonal question closing later.
        burst = [_question(87000 + i, "binary", minutes=60) for i in range(40)]
        records, planner, calls = run_phase([_question(86999, "binary", minutes=600)], burst, _budget(day=8.0))
        out["R"] = {"infos": [_info(r, planner) for r in records], "planner": planner, "calls": calls}
        phase("J", 90000, _budget(), fail={m: lambda: litellm.ServiceUnavailableError(
            message="rehearsal: overloaded", llm_provider="gemini", model="gemini") for m in bot_config.GEMINI_FORECAST_MODELS})

        def agent_phase(name: str, base: int, claude: ClaudeBudget, fake: RecordedAnthropic, prespent: tuple = (),
                        seasonal_agents: int = 1) -> None:
            seasonal = [_question(base + i, kind) for i, kind in enumerate(SEASONAL_TYPES)]
            minibench = [_question(base + 9, "binary")]
            for post, dollars in prespent:
                claude.questions[str(post)] = {"spent": dollars}
            records, planner, _ = run_agent_phase(seasonal, minibench, claude, fake, seasonal_agents)
            out[name] = {"infos": [_info(r, planner) for r in records], "planner": planner, "calls": [], "fake": fake,
                         "report": frontier.frontier_report(planner)}

        agent_phase("K", 81000, _budget(), RecordedAnthropic("full"))
        # The 7-minute cap on a 1:100 clock (4.2 s; planned wait 5.4 s, 1.2 s kept for the fallback).
        for attr in AGENT_TIMES:
            times[attr] = getattr(frontier, attr)
            setattr(frontier, attr, getattr(frontier, attr) / 100)
        main.PLANNED_WAIT_SECONDS = wait / 100
        agent_phase("L", 82000, _budget(), RecordedAnthropic("full", agent_delay=30.0))
        agent_phase("L2", 83000, _budget(), RecordedAnthropic("full", agent_delay=30.0, plain_delay=30.0))
        agent_phase("S2", 91000, _budget(), RecordedAnthropic("full", agent_delay=30.0), seasonal_agents=2)
        for attr, value in times.items():
            setattr(frontier, attr, value)
        main.PLANNED_WAIT_SECONDS = wait
        agent_phase("M", 84000, _budget(), RecordedAnthropic("tool-errors"))
        agent_phase("N", 85000, _budget(), RecordedAnthropic("near-caps"))
        agent_phase("N2", 86000, _budget(), RecordedAnthropic("caps-used"))
        store = MemoryStore({"days": {}})
        agent_phase("O", 87000, _budget(store=store), RecordedAnthropic("credit"))
        out["O_next"] = frontier.claude_mode(ClaudeBudget(store, BUDGET, RESET))
        agent_phase("P", 88000, _budget(), RecordedAnthropic("full"),
                    prespent=tuple((88000 + i, 1.00) for i in range(4)) + ((88009, 0.80),))
        agent_phase("Q", 89000, _budget(day=14.00), RecordedAnthropic("full"))
        agent_phase("S", 90000, _budget(), RecordedAnthropic("full"), seasonal_agents=2)
        agent_phase("U1", 76000, _paced(15.0), RecordedAnthropic("full"), seasonal_agents=2)
        agent_phase("U2", 75000, _paced(25.0), RecordedAnthropic("full"), seasonal_agents=2)
        return out
    finally:
        for attr, value in times.items():
            setattr(frontier, attr, value)
        main.PLANNED_WAIT_SECONDS = wait
        main.QUICK_FORECAST_RETRY_WAIT_SECONDS = quick_wait
        _RecordedAnswer.binary_percent = {}


def agent_checks(out: dict, infos: Any) -> list[tuple[str, bool]]:
    def runs(phase: str) -> list[dict]:
        return [a for i in infos(phase) for a in i["agent_runs"]]

    def agent_calls(phase: str) -> list[dict]:
        claude = out[phase]["planner"].claude
        return [c for e in claude.questions.values() for c in e.get("calls", []) if c["model"] == AGENT]

    def reqs(phase: str) -> list[dict]:
        return out[phase]["fake"].agent_requests

    k_s, k_m = infos("K", True), infos("K", False)
    k_runs, k_planner = runs("K"), out["K"]["planner"]
    continued = [r for r in reqs("K") if r["messages"][-1]["role"] == "assistant"]
    o_fake, q_day = out["O"]["fake"], out["Q"]["planner"].claude.day_spent()
    s_reserve = frontier.agent_reserve(True)
    two_reserve = s_reserve + frontier.agent_reserve(True, AGENT2)
    s_planner, s2_day = out["S"]["planner"], out["S2"]["planner"].claude.day_spent()
    s2_calls = [c for e in out["S2"]["planner"].claude.questions.values() for c in e.get("calls", []) if c["model"] in (AGENT, AGENT2)]
    return [
        ("K: seasonal, every type = agent + Opus 5.5 + Flash x2 (4 forecasts)",
         len(k_s) == 4 and all(i["forecasts"] == 4 and i["agent"] == 1 and i["opus"] == 1 and i["flash"] == 2 for i in k_s)),
        ("K: MiniBench = agent + Opus 5.5 + Flash x2 (4 forecasts)",
         all(i["forecasts"] == 4 and i["agent"] == 1 and i["opus"] == 1 and i["flash"] == 2 for i in k_m)),
        ("K: pause_turn continued with the paused content, no extra user message (2 requests per agent turn)",
         len(k_runs) == 5 and all(r["requests"] == 2 for r in k_runs) and len(continued) == 5
         and all(len(r["messages"]) == 2 for r in continued)),
        ("K: tools web_search_20260209 / web_fetch_20260209, max_uses 8/3 seasonal and 3/2 MiniBench, max_content_tokens set",
         {(r["tools"][0]["type"], r["tools"][1]["type"]) for r in reqs("K")} == {("web_search_20260209", "web_fetch_20260209")}
         and {(r["tools"][0]["max_uses"], r["tools"][1]["max_uses"]) for r in reqs("K") if r["messages"][-1]["role"] == "user"} == {(8, 3), (3, 2)}
         and all(r["tools"][1]["max_content_tokens"] == frontier.AGENT_FETCH_MAX_CONTENT_TOKENS for r in reqs("K"))),
        ("K: private log: URLs read, 3 searches, 1 fetch, seconds and cost (incl. $0.01 per search) per agent turn",
         all(r["urls_read"] == [AGENT_URL] and r["searches"] == 3 and r["fetches"] == 1 and r["seconds"] >= 0
             and abs(r["cost"] - (2 * (9000 * 4 + 2500 * 20) / 1e6 + 0.03)) < 1e-9 for r in k_runs)),
        (f"K: Claude caps raised while the agent runs (${frontier.AGENT_SEASONAL_QUESTION_CAP:.2f} / ${frontier.AGENT_MINIBENCH_QUESTION_CAP:.2f}) and kept",
         all(k_planner.claude.caps[str(i["post"])] == (frontier.AGENT_SEASONAL_QUESTION_CAP if i["seasonal"] else frontier.AGENT_MINIBENCH_QUESTION_CAP)
             and i["claude_spent"] <= k_planner.claude.caps[str(i["post"])] + 1e-9 for i in infos("K"))),
        ("K: step summary has the agent numbers (5 calls, 15 searches, 5 fetches) and no URL",
         "| 5 | 5 | 15 | 5 |" in out["K"]["report"] and "example.org" not in out["K"]["report"]),
        ("L: agent past its cap -> cancelled, charged its worst case, the plain Opus fallback answers in the wait",
         all(r["outcome"] == "timed out" for r in runs("L")) and len(runs("L")) == 4
         and all(abs(c["charged"] - s_reserve) < 1e-9 and not c["ok"] for c in agent_calls("L"))
         and all(i["submitted"] and i["opus"] == 2 and i["agent"] == 0 for i in infos("L", True))),
        (f"L: 4 seasonal agents running hold ${4 * s_reserve:.2f} > the $9 MiniBench limit -> MiniBench today's setup (Flash), forecast",
         all(i["submitted"] and i["claude"] == 0 and i["flash"] >= 3 for i in infos("L", False))),
        ("L2: agent and plain Opus both too slow -> every question still forecast (Flash)",
         all(i["submitted"] and i["claude"] == 0 and i["flash"] == 2 for i in infos("L2", True))
         and all(i["submitted"] for i in infos("L2", False))),
        ("M: tool failures counted in the private log, the agent's answer kept",
         all(i["agent"] == 1 and i["submitted"] for i in infos("M"))
         and all(r["tool_errors"] == {"web_search:unavailable": 1, "web_fetch:url_not_accessible": 1, "web_fetch:unsupported_content_type": 1}
                 for r in runs("M"))),
        ("N: one search and one fetch left at a pause -> continued with max_uses 1 / 1; caps never passed",
         all(i["agent"] == 1 for i in infos("N"))
         and all((r["tools"][0]["max_uses"], r["tools"][1]["max_uses"]) == (1, 1) for r in reqs("N") if r["messages"][-1]["role"] == "assistant")
         and all(r["searches"] <= (8 if r["reserve"] > 1 else 4) and r["fetches"] <= (5 if r["reserve"] > 1 else 3) for r in runs("N"))),
        ("N2: every search and fetch used at a pause -> not continued, plain Opus answers",
         all(r["outcome"] == "paused, not continued (tool caps)" and r["requests"] == 1 for r in runs("N2"))
         and all(i["agent"] == 0 and i["opus"] == 2 and i["submitted"] for i in infos("N2"))),
        (f"O: credit error on the agent: never retried, no fallback ({len(o_fake.agent_requests)} agent + {o_fake.plain_requests} plain request(s))",
         len(o_fake.agent_requests) + o_fake.plain_requests <= 2 and all(i["submitted"] for i in infos("O"))
         and not any(c["model"] in (OPUS_5, HAIKU) for e in out["O"]["planner"].claude.questions.values() for c in e.get("calls", []))),
        (f"O: the next run: {out['O_next'][0]} ({out['O_next'][1]})", out["O_next"][0] == "off"),
        ("P: question near its cap -> no agent; seasonal plain Opus / Haiku within $1.50; MiniBench today's setup; all forecast",
         all(i["agent"] == 0 and i["submitted"] for i in infos("P"))
         and all(i["claude"] >= 1 and i["claude_spent"] <= frontier.CLAUDE_SEASONAL_QUESTION_CAP + 1e-9 for i in infos("P", True))
         # MiniBench at $0.80: the agent's $0.87 passes $1.60 and plain Opus's $0.60 cap is already passed.
         and all(i["claude"] == 0 for i in infos("P", False))),
        (f"Q: Claude day at $14.00 -> no seasonal agent ($2.88 would pass $15), plain Opus; MiniBench no Claude (over $9); ${q_day:.2f} <= $15.00",
         all(i["agent"] == 0 and i["opus"] >= 1 and i["submitted"] for i in infos("Q", True))
         and all(i["claude"] == 0 and i["submitted"] for i in infos("Q", False))
         and q_day <= frontier.CLAUDE_DAILY_CAP + 1e-9),
        ("S: two seasonal agents (test switch 2) -> seasonal = agent + agent2 + Flash x2; MiniBench = agent + Opus + Flash x2; caps $5.30 / $1.60; day cap $20",
         all(i["forecasts"] == 4 and i["agent"] == 1 and i["agent2"] == 1 and i["flash"] == 2 and i["submitted"] for i in infos("S", True))
         and all(i["agent"] == 1 and i["opus"] == 1 and i["agent2"] == 0 and i["submitted"] for i in infos("S", False))
         and all(s_planner.claude.caps[str(i["post"])] == (frontier.AGENT2_SEASONAL_QUESTION_CAP if i["seasonal"]
                 else frontier.AGENT_MINIBENCH_QUESTION_CAP) for i in infos("S"))
         and all(i["claude_spent"] <= frontier.AGENT2_SEASONAL_QUESTION_CAP + 1e-9 for i in infos("S"))
         and s_planner.claude.daily_cap == frontier.CLAUDE_DAILY_CAP_TWO_AGENTS
         # First requests (continuations get what is left): agent 8/3, agent2 4/2, MiniBench agent 3/2.
         and {(8, 3), (4, 2), (3, 2)} <= {tuple(t["max_uses"] for t in r["tools"]) for r in reqs("S")}),
        (f"S2: both agents past their cap -> both cancelled at their worst case, both Opus fallbacks answer inside the wait; "
         f"8 running agents hold ${4 * two_reserve:.2f} > $9: MiniBench today's setup; ${s2_day:.2f} <= $20.00",
         len(runs("S2")) == 8 and all(r["outcome"] == "timed out" for r in runs("S2"))
         and len([c for c in s2_calls if c["model"] == AGENT2]) == 4 and not any(c["ok"] for c in s2_calls)
         and all(i["submitted"] and i["opus"] == 2 and i["agent"] == 0 and i["agent2"] == 0 for i in infos("S2", True))
         and all(i["submitted"] and i["claude"] == 0 for i in infos("S2", False))
         and all(i["claude_spent"] >= two_reserve - 1e-9 and i["claude_spent"] <= frontier.AGENT2_SEASONAL_QUESTION_CAP + 1e-9
                 for i in infos("S2", True))
         and s2_day <= frontier.CLAUDE_DAILY_CAP_TWO_AGENTS + 1e-9),
        ("U1: paced-1 (cycle spend line + $15) -> seasonal unchanged (agent + agent2 + Flash x2, $5.30); "
         "MiniBench plain Opus + Flash x2 ($0.60)",
         out["U1"]["planner"].pace == "paced-1" and out["U1"]["planner"].mode == "full"
         and all(i["forecasts"] == 4 and i["agent"] == 1 and i["agent2"] == 1 and i["flash"] == 2 and i["submitted"] for i in infos("U1", True))
         and all(i["forecasts"] == 3 and i["agent"] == 0 and i["opus"] == 1 and i["flash"] == 2 and i["submitted"] for i in infos("U1", False))
         and all(out["U1"]["planner"].claude.caps[str(i["post"])] == (frontier.AGENT2_SEASONAL_QUESTION_CAP if i["seasonal"]
                 else frontier.CLAUDE_MINIBENCH_QUESTION_CAP) for i in infos("U1"))),
        ("U2: paced-2 (line + $25) -> seasonal agent + plain Opus + Flash x2 ($3.60), no agent2; MiniBench plain Opus + Flash x2; day cap $20",
         out["U2"]["planner"].pace == "paced-2" and out["U2"]["planner"].mode == "full"
         and all(i["forecasts"] == 4 and i["agent"] == 1 and i["agent2"] == 0 and i["opus"] == 1 and i["flash"] == 2 and i["submitted"]
                 for i in infos("U2", True))
         and all(i["forecasts"] == 3 and i["agent"] == 0 and i["opus"] == 1 and i["flash"] == 2 and i["submitted"] for i in infos("U2", False))
         and all(out["U2"]["planner"].claude.caps[str(i["post"])] == (frontier.AGENT_SEASONAL_QUESTION_CAP if i["seasonal"]
                 else frontier.CLAUDE_MINIBENCH_QUESTION_CAP) for i in infos("U2"))
         and out["U2"]["planner"].claude.daily_cap == frontier.CLAUDE_DAILY_CAP_TWO_AGENTS),
        ("T: free Flash quota at 0 with Claude on -> 2 paid Flash per question (seasonal and MiniBench), within $0.10 a question",
         all(i["submitted"] and i["claude"] >= 1 and i["paid_flash"] == 2 for i in infos("T"))
         and all(i["openrouter_spent"] <= bot_config.PAID_FLASH_QUESTION_CAP + 1e-9 for i in infos("T"))
         and out["T"]["planner"].spend.day_spent() <= bot_config.PAID_FLASH_DAILY_CAP + 1e-9),
        ("T2: same, paid day at $0.98 -> the $1.00 day cap refuses paid Flash; Claude still forecasts every question",
         all(i["submitted"] and i["claude"] >= 1 and i["paid_flash"] == 0 for i in infos("T2"))
         and out["T2"]["planner"].spend.day_spent() <= bot_config.PAID_FLASH_DAILY_CAP + 1e-9),
        ("T3: same, paid day at $0.48 -> seasonal gets paid Flash; MiniBench refused (paid only under $0.50)",
         all(i["paid_flash"] >= 1 for i in infos("T3", True)) and all(i["submitted"] and i["paid_flash"] == 0 for i in infos("T3", False))),
        ("Agent phases: 0 real calls (every Anthropic request went to the recorded client)",
         all(out[p]["fake"].agent_requests or out[p]["fake"].plain_requests for p in ("K", "L", "M", "N", "N2", "O", "P", "Q", "S", "S2", "U1", "U2"))),
    ]


def report() -> tuple[bool, str]:
    # A-J check the plain lineup: the live agent switch is pinned off here;
    # the agent phases set the planner's switch themselves.
    agent_switch, bot_config.FRONTIER_AGENT_ENABLED = bot_config.FRONTIER_AGENT_ENABLED, False
    try:
        out = run()
    finally:
        bot_config.FRONTIER_AGENT_ENABLED = agent_switch

    def infos(phase: str, seasonal: bool | None = None) -> list[dict]:
        return [i for i in out[phase]["infos"] if seasonal is None or bool(i["seasonal"]) == seasonal]

    def mode(phase: str) -> str:
        return out[phase]["planner"].mode

    frontier_phases = ("A", "B", "E", "F", "H", "I", "J")
    a_s, a_m = infos("A", True), infos("A", False)
    d_claude_calls = [m for m in out["D"]["calls"] if m.startswith("anthropic/")]
    f_day = out["F"]["planner"].claude.day_spent()
    r_day = out["R"]["planner"].claude.day_spent()
    r_mini_spent = 8.0 + sum(i["claude_spent"] for i in infos("R", False))
    r_mini_claude = sum(1 for i in infos("R", False) if i["claude"])
    checks = [
        ("A: seasonal, every type = Opus 5.5 x2 + Flash x2 (4 forecasts)",
         len(a_s) == 4 and all(i["forecasts"] == 4 and i["opus"] == 2 and i["flash"] == 2 for i in a_s)),
        ("A: MiniBench = Opus 5.5 + Flash x2 (3 forecasts)", all(i["forecasts"] == 3 and i["opus"] == 1 and i["flash"] == 2 for i in a_m)),
        ("A: binary questions with round 1 disagreeing: no round 2 (seasonal and MiniBench)",
         all(not i["round2"] for i in infos("A") if i["type"] == "binary")),
        (f"A: Claude <= ${frontier.CLAUDE_SEASONAL_QUESTION_CAP:.2f} seasonal / ${frontier.CLAUDE_MINIBENCH_QUESTION_CAP:.2f} MiniBench per question",
         all(i["claude_spent"] <= frontier.CLAUDE_SEASONAL_QUESTION_CAP + 1e-9 for i in a_s)
         and all(i["claude_spent"] <= frontier.CLAUDE_MINIBENCH_QUESTION_CAP + 1e-9 for i in a_m)),
        ("Never more than 2 Flash on a question with a Claude forecast (A, B, F, H, I, J)",
         all(i["flash"] <= 2 for p in ("A", "B", "F", "H", "I", "J") for i in infos(p) if i["claude"])),
        ("B (20% left): seasonal Opus x1 + Flash x2; MiniBench Opus x1 + Flash x2",
         mode("B") == "low" and all(i["opus"] == 1 and i["flash"] == 2 for i in infos("B"))),
        ("C (5% left): seasonal Opus x1 + Flash x2; MiniBench no Claude (today's setup)",
         mode("C") == "minimal" and all(i["opus"] == 1 and i["flash"] == 2 for i in infos("C", True))
         and all(i["claude"] == 0 and i["flash"] >= 3 for i in infos("C", False))),
        (f"D: credit error: {len(d_claude_calls)} Claude call(s), never retried, no Claude backup ({', '.join(_short(m) for m in d_claude_calls)})",
         OPUS_5 not in d_claude_calls and HAIKU not in d_claude_calls and len(d_claude_calls) <= 2),
        ("D: every question still forecast (today's setup)", all(i["submitted"] for i in infos("D"))),
        (f"D: the next run: {out['D_next'][0]} ({out['D_next'][1]})", out["D_next"][0] == "off"),
        ("E: GPT on and failing -> its slot answered by Flash, GPT called once per seasonal question",
         all(i["forecasts"] == 5 and i["gpt"] == 0 and i["flash"] == 3 for i in infos("E", True))
         and out["E"]["calls"].count(GPT_SOL) == len(infos("E", True))),
        (f"F: Claude day at $14.50 -> ${f_day:.2f} <= $15.00; every question forecast",
         f_day <= frontier.CLAUDE_DAILY_CAP + 1e-9 and all(i["submitted"] for i in infos("F"))),
        ("F: an Opus slot refused by the day cap passed to a backup that fits (Haiku)", HAIKU in out["F"]["calls"]),
        ("G: ledger unreadable -> no Claude (today's setup)",
         mode("G") == "off" and all(i["claude"] == 0 and i["submitted"] for i in infos("G"))),
        ("H: GPT on, key under $2 -> no GPT; Opus x2 + Flash x2",
         all(i["gpt"] == 0 and i["opus"] == 2 and i["flash"] == 2 for i in infos("H", True)) and GPT_SOL not in out["H"]["calls"]),
        ("I: Opus slower than the planned wait -> cut; every question forecast with Flash",
         all(i["submitted"] and i["opus"] == 0 for i in infos("I"))),
        ("J: every free Flash 503 -> both Flash slots paid, with the Opus forecasts in",
         all(i["paid_flash"] == 2 and i["opus"] >= 1 for i in infos("J"))),
        (f"R: 40-question MiniBench burst from $8: MiniBench Claude stopped by ${frontier.CLAUDE_MINIBENCH_DAY_LIMIT:.0f} "
         f"({r_mini_claude} with Claude), day ${r_day:.2f} <= ${frontier.CLAUDE_DAILY_CAP:.0f}",
         r_mini_spent <= frontier.CLAUDE_MINIBENCH_DAY_LIMIT + 1e-9 and r_day <= frontier.CLAUDE_DAILY_CAP + 1e-9
         and 0 < r_mini_claude < 40 and len(infos("R")) == 41 and all(i["submitted"] for i in infos("R"))),
        ("R: the seasonal question after the burst still gets both Opus slots",
         [i["opus"] for i in infos("R", True)] == [2]),
        ("Modes: A, B, E, F, H, I, J frontier; G off", all(mode(p) != "off" for p in frontier_phases) and mode("G") == "off"),
        *agent_checks(out, infos),
    ]
    ok = all(c for _, c in checks)
    lines = ["## Frontier lineup rehearsal (recorded replies, 0 model calls, $0)", "",
             f"Budget ${BUDGET:.0f} per cycle; caps ${frontier.CLAUDE_DAILY_CAP:.0f}/UTC day, "
             f"${frontier.CLAUDE_SEASONAL_QUESTION_CAP:.2f} seasonal / ${frontier.CLAUDE_MINIBENCH_QUESTION_CAP:.2f} MiniBench "
             f"per question (Claude); at most {frontier.FLASH_FORECASTS} Flash per question.", ""]
    titles = {"A": "A. Full", "B": "B. Under 25% left", "C": "C. Under 10% left", "D": "D. Credit balance too low",
              "E": "E. GPT on and failing", "F": "F. Claude day at $14.50", "G": "G. Claude ledger unreadable",
              "H": "H. GPT on, OpenRouter key under $2", "I": "I. Opus slower than the planned wait",
              "J": "J. Every free Flash 503", "K": "K. Agent: full path", "L": "L. Agent past its 7-minute cap",
              "L2": "L2. Agent and plain Opus too slow", "M": "M. Agent tool failures", "N": "N. Agent near its tool caps",
              "N2": "N2. Agent tool caps used at a pause", "O": "O. Agent credit error", "P": "P. Question near its cap",
              "Q": "Q. Claude day at $14.00", "R": "R. MiniBench burst of 40 from a $8 Claude day",
              "S": "S. Two seasonal agents (test switch)", "T": "T. Free Flash quota at 0: paid Flash planned",
              "T2": "T2. Free Flash at 0, paid day at $0.98", "T3": "T3. Free Flash at 0, paid day at $0.48", "S2": "S2. Two seasonal agents both past their cap",
              "U1": "U1. Pacing: paced-1 (line + $15)", "U2": "U2. Pacing: paced-2 (line + $25)"}
    for phase, title in titles.items():
        lines += [f"### {title} (mode {mode(phase)})", "", *_rows(out[phase]["infos"]), ""]
    lines += [f"- {text}: **{'yes' if good else 'NO'}**" for text, good in checks]
    return ok, "\n".join(lines)


if __name__ == "__main__":
    ok, text = report()
    print(text)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    sys.exit(0 if ok else 1)
