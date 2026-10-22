"""
Frontier lineup (architect, 21 Oct; PR 2, built OFF behind
bot_config.FRONTIER_ENABLED). Claude through ANTHROPIC_API_KEY (the Console
credits that come with Tony's Max plan: no card, no auto-reload; when they are
used up, calls fail with "credit balance is too low" and are never charged)
and today's Gemini Flash path. Second version (architect, 21 Oct, from the
Fall leaderboard: Opus 5.5 beats Fable 5.1 and Sonnet 5.5; GPT-5.6 Sol doubled
in price):

- Seasonal: Opus 5.5 (effort high) x2 + Flash x2 (free key first, then paid
  Flash), every question type, no round 2.
- MiniBench: Opus 5.5 x1 + Flash x2, no round 2.
- At most 2 Flash forecasts per question (no extra-forecast top-ups: Flash
  never outvotes Claude); paid Flash only to reach those 2. A question with no
  Claude slot (a step-down, every slot refused by a cap) gets today's setup.
- Backups: Opus 5.5 -> Opus 5 -> Haiku 5.5 (a cap-refused backup passes to the
  next). Claude errors are never retried; a credit error is never retried and
  switches Claude off until the next cycle.
- GPT-5.6 Sol: behind bot_config.FRONTIER_GPT_ENABLED (OFF). When on: one
  seasonal slot; any failure -> the Flash chain; $0.30 per seasonal question
  (GPT + paid Flash); none when the OpenRouter key has under $2 left.
- Budget (ClaudeBudget, fall26-data status/claude_spend.json): one setting,
  CLAUDE_BUDGET_PER_CYCLE, plus the cycle reset date; ledger caps $15 per UTC
  day (MiniBench only under $9), $1.50 per seasonal question, $0.60 per MiniBench question. Each call
  reserves its worst case (max_tokens of output), so a cap is never passed.
- Step-down (claude_mode, once per run): under 25% of the cycle budget left ->
  seasonal Opus x1; under 10% -> seasonal Opus x1, no Claude on MiniBench; a
  credit error, an unreadable ledger or no reset date -> no Claude: today's setup.
- Agent (PR 4, behind bot_config.FRONTIER_AGENT_ENABLED, ON): in full mode one
  Opus slot becomes an agent (Opus 5.5 with Anthropic's web_search + web_fetch
  server tools, same prompt and answer format, the dossier as its start).
  Caps: 8 searches / 3 fetches seasonal, 3 / 2 MiniBench, 3,000 tokens a page,
  7 minutes, then the plain Opus chain with the time left. Question caps
  $3.00 / $1.60 while it runs; an overrun: agent off for the run + an alert.
  Low and minimal modes: plain Opus only (the agent is the dear slot).
"""
from __future__ import annotations

import asyncio
import calendar
import contextvars
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from forecasting_tools import GeneralLlm
from forecasting_tools.ai_models.ai_utils.response_types import TextTokenCostResponse


class ClaudeResponse(TextTokenCostResponse):
    """A Claude answer, with the API's stop reason (for the ledger)."""

    stop_reason: str | None = None

import bot_config
import ensemble
from bot_helpers import PUBLIC_LOGGER_NAME
from gemini_budget import current_question_key
from llm_throttle import NoAnswer, RequestPacer, ThrottledLlm
from spend import SpendGuard, _alert, http_status, question_seasonal, utc_day

logger = logging.getLogger(PUBLIC_LOGGER_NAME)

OPUS_55 = "anthropic/claude-opus-5-5"
OPUS_5 = "anthropic/claude-opus-5"
HAIKU = "anthropic/claude-haiku-5-5"
GPT_SOL = ensemble.GPT_SOL  # openrouter/openai/gpt-5.6-sol

CLAUDE_BACKUPS: dict[str, list[str]] = {OPUS_55: [OPUS_5, HAIKU]}

# Anthropic first-party prices, dollars per million tokens (input, output),
# from the Claude API reference (cached 6 Oct 2026). Confirmed by the pre-flight.
CLAUDE_PRICES: dict[str, tuple[float, float]] = {
    OPUS_55: (4.00, 20.00),
    OPUS_5: (5.00, 25.00),
    HAIKU: (0.10, 0.50),
}
CLAUDE_EFFORT = "high"
# Output cap per call (thinking included). Each call reserves its worst case:
# CLAUDE_RESERVE_PROMPT_TOKENS in, CLAUDE_MAX_TOKENS out (Opus 5.5 $0.36,
# Opus 5 $0.45, Haiku $0.01): 2 Opus 5.5 fit the $1.50 seasonal cap and 1 the
# $0.60 MiniBench cap.
CLAUDE_MAX_TOKENS = 16000
CLAUDE_RESERVE_PROMPT_TOKENS = 10000
# One call at most 6 minutes (the planned-forecast wait is 9): a backup still has time.
CLAUDE_TIMEOUT_SECONDS = 360
GPT_TIMEOUT_SECONDS = 360
CLAUDE_REQUESTS_PER_MINUTE = 50

CLAUDE_SPEND_PATH = "status/claude_spend.json"
CLAUDE_DAILY_CAP = 15.00
# MiniBench (or an unknown tournament) may use Claude only while the UTC day's
# Claude spend, running reservations included, stays under this: at least
# $6 a day is always left for seasonal questions (architect, 21 Oct).
CLAUDE_MINIBENCH_DAY_LIMIT = 9.00
CLAUDE_SEASONAL_QUESTION_CAP = 1.50
CLAUDE_MINIBENCH_QUESTION_CAP = 0.60
OPENROUTER_SEASONAL_QUESTION_CAP = 0.30
GPT_MIN_KEY_LEFT = 2.00  # dollars left on the OpenRouter key; under it, no GPT
STEP_DOWN_LOW = 0.25
STEP_DOWN_MINIMAL = 0.10
FLASH_FORECASTS = 2  # per question, at most, while a Claude slot runs
CYCLES_KEPT = 3

# The Claude slots per mode: (seasonal, MiniBench). GPT (when switched on) is
# added on seasonal.
MODE_SLOTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "full": ((OPUS_55, OPUS_55), (OPUS_55,)),
    "low": ((OPUS_55,), (OPUS_55,)),
    "minimal": ((OPUS_55,), ()),
}
CREDIT_ERROR_TEXT = "credit balance is too low"

# ---- the agent (PR 4, OFF): Opus 5.5 + web_search / web_fetch (server tools).
AGENT = "anthropic/claude-opus-5-5-agent"  # ledger name; the call is Opus 5.5
# The second seasonal agent (bot_config.FRONTIER_SEASONAL_AGENTS = 2; built OFF): smaller caps.
AGENT2 = "anthropic/claude-opus-5-5-agent2"
AGENTS = (AGENT, AGENT2)
AGENT_SEARCH_TOOL = "web_search_20260209"  # current versions for Opus 5.5 (dynamic filtering)
AGENT_FETCH_TOOL = "web_fetch_20260209"
# Question pages show the community prediction, which we don't use (architect, 21 Oct).
AGENT_BLOCKED_DOMAINS = ("metaculus.com",)
AGENT_SEARCH_PRICE = 0.01  # dollars per search ($10 per 1,000)
# Page text per fetch, in tokens (about 12 kB; PDFs are not cut by it).
AGENT_FETCH_MAX_CONTENT_TOKENS = 3000
AGENT_TIMEOUT_SECONDS = 420  # 7 minutes, then the plain Opus chain
AGENT_FALLBACK_SECONDS = 120  # kept for the plain Opus fallback inside the planned wait
AGENT_MIN_SECONDS = 60  # under this, no agent (the fallback runs at once)
PARSE_MARGIN_SECONDS = 20  # a deadline-aware fallback ends this long before the wait
FALLBACK_MIN_SECONDS = 45  # under this, the fallback is not started
AGENT_MAX_CONTINUATIONS = 3  # pause_turn continuations per agent turn
AGENT_MIN_OUTPUT_TOKENS = 2000  # a continuation needs at least this much output left
AGENT_SEASONAL_QUESTION_CAP = 3.60  # 22 Oct: agent $2.88 + plain Opus + its Opus fallback ($0.72)
# Two seasonal agents (architect, 21 Oct; 22 Oct: agent 600k input): agent $2.88 +
# agent2 $1.64 + their two Opus fallbacks ($0.72) = $5.24; the Claude day cap rises to $20 with them.
AGENT2_SEASONAL_QUESTION_CAP = 5.30
CLAUDE_DAILY_CAP_TWO_AGENTS = 20.00
AGENT_MINIBENCH_QUESTION_CAP = 1.60


@dataclass(frozen=True)
class AgentCaps:
    searches: int
    fetches: int
    output_tokens: int  # all requests of the turn together (thinking included)
    input_tokens: int  # ASSUMED ceiling, all server-side iterations together


# Worst case = input x $4 + output x $20 per million + searches x $0.01
# (architect, 21 Oct: the server-side loop re-reads its context, so a test
# call read 413k input tokens; the input ceilings are set from that):
# seasonal $1.80 + $0.40 + $0.08 = $2.28 (22 Oct: a real call read 538k input, so
# 600k: $2.40 + $0.40 + $0.08 = $2.88), so agent + plain Opus ($0.36) + the
# agent's Opus fallback ($0.36) = $3.60 (was $3.00 at 450k); MiniBench $0.60 + $0.24 + $0.03 =
# $0.87, + plain Opus ($0.36) + its fallback ($0.36) = $1.59 (cap $1.60;
# architect, 21 Oct: MiniBench gets a plain Opus slot next to the agent).
AGENT_CAPS = {
    True: AgentCaps(searches=8, fetches=3, output_tokens=20000, input_tokens=600000),
    False: AgentCaps(searches=3, fetches=2, output_tokens=12000, input_tokens=150000),
}
# agent2 (seasonal only): $1.20 + $0.40 + $0.04 = $1.64 (architect, 21 Oct: a test
# call read 215k input, so 300k; the dollar reservation is the guard).
AGENT2_CAPS = AgentCaps(searches=4, fetches=2, output_tokens=20000, input_tokens=300000)


def agent_caps(seasonal: bool, name: str = AGENT) -> AgentCaps:
    return AGENT2_CAPS if name.removeprefix("replay/") == AGENT2 else AGENT_CAPS[seasonal]
AGENT_SYSTEM = (
    "You are forecasting the question in the user message. The research assistant's report in it is "
    "your starting point. You have web_search (at most {searches} searches) and web_fetch (at most "
    "{fetches} pages) to check the key facts, find newer information and read the resolution source. "
    "If Polymarket, Kalshi or Manifold has a market on the same event, check its current price and weigh it. "
    "Look for a base rate: how often similar events happened in similar time windows. "
    "Then write your answer exactly as the user message asks, ending with the final answer in the format "
    "it gives."
)

# Set per forecast by main.py: the loop-independent deadline (time.monotonic)
# of the planned wait, and the agent runs of this forecast (private log).
forecast_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("forecast_deadline", default=None)
agent_log: contextvars.ContextVar[list | None] = contextvars.ContextVar("agent_log", default=None)


def claude_reserve(model: str) -> float:
    prompt, completion = CLAUDE_PRICES[model.removeprefix("replay/")]
    return (CLAUDE_RESERVE_PROMPT_TOKENS * prompt + CLAUDE_MAX_TOKENS * completion) / 1_000_000


def agent_reserve(seasonal: bool, name: str = AGENT) -> float:
    """The agent's worst case: its token and search caps at Opus 5.5 prices."""
    caps, (prompt, completion) = agent_caps(seasonal, name), CLAUDE_PRICES[OPUS_55]
    return (caps.input_tokens * prompt + caps.output_tokens * completion) / 1_000_000 + caps.searches * AGENT_SEARCH_PRICE


def agent_seasonal() -> bool:
    """The question in this task is seasonal; unknown -> seasonal (the larger reserve: fail closed)."""
    return question_seasonal.get() is not False


def seconds_left() -> float | None:
    deadline = forecast_deadline.get()
    return None if deadline is None else deadline - time.monotonic()


def claude_cost(model: str, usage: Any) -> float:
    """Dollars for one answered call, from its token usage."""
    prompt, completion = CLAUDE_PRICES[model.removeprefix("replay/")]
    fresh = getattr(usage, "input_tokens", 0) or 0
    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    return ((fresh + 1.25 * written + 0.1 * read) * prompt + out * completion) / 1_000_000


def is_credit_error(error: BaseException | None) -> bool:
    """The Anthropic 'credit balance is too low' error (HTTP 400). Only the
    status and this phrase are looked at; the message is never logged."""
    current, seen = error, 0
    while current is not None and seen < 6:
        if CREDIT_ERROR_TEXT in str(current).lower() and http_status(current) in (400, None):
            return True
        current, seen = current.__cause__ or current.__context__, seen + 1
    return False


# ---------------------------------------------------------------- the billing cycle


def cycle_start(today: date, reset: date | None) -> date | None:
    """The start of the billing cycle `today` is in: the latest day on or
    before today with the reset date's day of month (clamped to short months)."""
    if reset is None:
        return None

    def on(year: int, month: int) -> date:
        return date(year, month, min(reset.day, calendar.monthrange(year, month)[1]))

    start = on(today.year, today.month)
    if start > today:
        year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        start = on(year, month)
    return start


def reset_date(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


class ClaudeBudget(SpendGuard):
    """The Claude spend ledger: SpendGuard's days, questions and caps, plus the
    spend per billing cycle and a credit error seen this cycle."""

    # Anthropic bills no request that ended in an API error (5xx, 529 overloaded).
    no_charge_statuses = (*SpendGuard.no_charge_statuses, 500, 502, 503, 504, 529)

    def __init__(self, store: Any, budget: float, reset: date | None, today: date | None = None) -> None:
        super().__init__(store)
        self.budget = budget
        self.today = today or datetime.now(timezone.utc).date()
        self.cycle = cycle_start(self.today, reset)
        self.cycles: dict[str, float] = {k: float(v) for k, v in (self.raw.get("cycles") or {}).items()}
        self.credit_errors: dict[str, str] = dict(self.raw.get("credit_errors") or {})  # cycle -> day
        self.daily_cap = CLAUDE_DAILY_CAP
        self.minibench_daily_cap = CLAUDE_MINIBENCH_DAY_LIMIT
        self.estimates = {m: claude_reserve(m) for m in CLAUDE_PRICES}
        self.agent_stats: dict[tuple, dict] = {}  # (question, agent) -> searches, fetches of the call finishing
        self.agent_off: str | None = None  # set: no agent for the rest of this run
        self.agent_overruns = 0  # agent calls over their reservation this run

    def estimate(self, model: str) -> float:
        name = model.removeprefix("replay/")
        if name in AGENTS:
            return agent_reserve(agent_seasonal(), name)
        return claude_reserve(name) if name in CLAUDE_PRICES else super().estimate(model)

    def note_agent(self, question: str | None, stats: dict, agent: str = AGENT) -> None:
        """The agent call that is about to finish: searches, fetches, pauses,
        tokens, outcome, stop reason, tool error codes (numbers and labels only)."""
        if question is not None:
            self.agent_stats[(question, agent.removeprefix("replay/"))] = stats

    def cycle_spent(self) -> float:
        return self.cycles.get(self.cycle.isoformat(), 0.0) if self.cycle else 0.0

    def fraction_left(self) -> float:
        return (self.budget - self.cycle_spent()) / self.budget if self.budget > 0 else 0.0

    def credit_error_this_cycle(self) -> bool:
        return self.cycle is not None and self.cycle.isoformat() in self.credit_errors

    def finish(self, model: str, succeeded: bool, cost: float | None = None, error: BaseException | None = None,
               timed_out: bool = False, **details: Any) -> None:
        before = self.day_spent()
        super().finish(model, succeeded, cost=cost, error=error, timed_out=timed_out, **details)
        question = current_question_key.get()
        name = model.removeprefix("replay/")
        stats = self.agent_stats.pop((question, name), None) if name in AGENTS else None
        if stats and question in self.questions and self.questions[question].get("calls"):
            self.questions[question]["calls"][-1].update(stats)
        if self.cycle is not None:
            key = self.cycle.isoformat()
            self.cycles[key] = self.cycles.get(key, 0.0) + (self.day_spent() - before)
        if not succeeded and is_credit_error(error):
            self.mark_credit_error()

    def mark_credit_error(self) -> None:
        """No Claude call for the rest of this cycle (never retried)."""
        if self.blocked is None:
            logger.warning("Claude: credit balance too low, no more Claude calls this cycle")
        self.blocked = "credit balance too low"
        if self.cycle is not None:
            self.credit_errors.setdefault(self.cycle.isoformat(), utc_day())

    def snapshot(self) -> dict:
        data = super().snapshot()
        kept = sorted(self.cycles)[-CYCLES_KEPT:]
        data["cycles"] = {k: round(self.cycles[k], 6) for k in kept}
        data["credit_errors"] = {k: v for k, v in self.credit_errors.items() if k in kept or k == (self.cycle and self.cycle.isoformat())}
        return data


def claude_mode(budget: ClaudeBudget | None, api_key: bool = True) -> tuple[str, str]:
    """(mode, why): 'full', 'low', 'minimal' or 'off' (today's setup)."""
    if budget is None:
        return "off", "frontier lineup off"
    if not api_key:
        return "off", "ANTHROPIC_API_KEY not set"
    if budget.load_failed:
        return "off", "Claude spend ledger could not be read"
    if budget.cycle is None:
        return "off", "billing cycle reset date not set"
    if budget.credit_error_this_cycle():
        return "off", "credit balance too low this cycle"
    left = budget.fraction_left()
    if left <= 0:
        return "off", "cycle budget used up"
    if left < STEP_DOWN_MINIMAL:
        return "minimal", f"{left:.0%} of the cycle budget left"
    if left < STEP_DOWN_LOW:
        return "low", f"{left:.0%} of the cycle budget left"
    return "full", f"{left:.0%} of the cycle budget left"


# ---------------------------------------------------------------- one Claude call


def _messages(prompt: Any) -> tuple[str | None, list[dict]]:
    """(system, messages) for the Messages API from a GeneralLlm prompt."""
    if isinstance(prompt, str):
        return None, [{"role": "user", "content": prompt}]
    system, messages = None, []
    for message in prompt:
        role, content = message.get("role"), message.get("content")
        if role == "system":
            system = content if system is None else f"{system}\n\n{content}"
        else:
            messages.append({"role": role, "content": content})
    return system, messages


class _ClaudeCall(GeneralLlm):
    """The model call at the bottom of a Claude ThrottledLlm: one request with
    the official Anthropic SDK (no SDK retries: the chain's backups handle
    failures), adaptive thinking (the models' default), effort high."""

    # True on the agent's fallback chain only: the call gets the planned
    # wait's time left (minus PARSE_MARGIN_SECONDS), not the full 6 minutes.
    deadline_aware = False

    def _request_timeout(self) -> float:
        left = seconds_left() if self.deadline_aware else None
        if left is None:
            return CLAUDE_TIMEOUT_SECONDS
        timeout = min(CLAUDE_TIMEOUT_SECONDS, left - PARSE_MARGIN_SECONDS)
        if timeout < FALLBACK_MIN_SECONDS:
            raise NoAnswer(f"{self.model}: no time left in the planned wait", cost=0.0)
        return timeout

    async def _mockable_direct_call_to_model(self, prompt: Any) -> TextTokenCostResponse:
        import anthropic

        timeout = self._request_timeout()
        system, messages = _messages(prompt)
        request: dict[str, Any] = dict(
            model=self.model.removeprefix("anthropic/"), max_tokens=CLAUDE_MAX_TOKENS,
            output_config={"effort": CLAUDE_EFFORT}, messages=messages,
        )
        if system:
            request["system"] = system
        async with anthropic.AsyncAnthropic(max_retries=0, timeout=timeout) as client:
            response = await client.messages.create(**request)
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        usage = response.usage
        if response.stop_reason == "refusal":
            raise NoAnswer(f"{self.model}: refused", cost=claude_cost(self.model, usage), stop_reason="refusal")
        if response.stop_reason == "max_tokens" or not text:
            raise NoAnswer(f"{self.model}: no answer (stop reason {response.stop_reason})", cost=claude_cost(self.model, usage),
                           stop_reason=response.stop_reason)
        return ClaudeResponse(stop_reason=response.stop_reason,
            data=text, prompt_tokens_used=usage.input_tokens, completion_tokens_used=usage.output_tokens,
            total_tokens_used=usage.input_tokens + usage.output_tokens, model=self.model,
            cost=claude_cost(self.model, usage),
        )


class ClaudeLlm(ThrottledLlm, _ClaudeCall):
    """A Claude slot: pacing, the Claude spend guard and backups (ThrottledLlm)
    around one Anthropic SDK call."""


# ---------------------------------------------------------------- the agent


class AgentTimeout(NoAnswer):
    """The agent ran past its time cap: cancelled, charged its worst case
    (cost None: the reservation), and the plain Opus chain answers."""


def _get(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


@dataclass
class AgentRun:
    """One agent turn (all its requests): counts, URLs and cost. URLs and
    tool error codes go to the private question log only, never stdout."""

    seasonal: bool
    name: str = AGENT
    requests: int = 0
    searches: int = 0
    fetches: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    last_input: int = 0
    token_cost: float = 0.0
    urls_read: list = field(default_factory=list)
    urls_found: list = field(default_factory=list)
    tool_errors: dict = field(default_factory=dict)
    stop: str | None = None
    outcome: str = "running"
    worst_case: bool = False  # charged the reservation (timed out / cut connection)
    seconds: float = 0.0
    texts: list = field(default_factory=list)

    @property
    def caps(self) -> AgentCaps:
        return agent_caps(self.seasonal, self.name)

    @property
    def cost(self) -> float:
        return self.token_cost + self.searches * AGENT_SEARCH_PRICE

    def add(self, response: Any) -> None:
        usage = response.usage
        self.requests += 1
        self.token_cost += claude_cost(OPUS_55, usage)
        tokens_in = sum(_get(usage, k, 0) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        self.input_tokens += tokens_in
        self.last_input = tokens_in
        self.output_tokens += _get(usage, "output_tokens", 0) or 0
        searches = fetches = 0
        for block in response.content or []:
            kind = _get(block, "type")
            if kind == "text":
                self.texts.append(_get(block, "text", "") or "")
            elif kind == "web_search_tool_result":
                content = _get(block, "content")
                if isinstance(content, list):
                    searches += 1
                    for hit in content:
                        url = _get(hit, "url")
                        if url and url not in self.urls_found and len(self.urls_found) < 40:
                            self.urls_found.append(url)
                else:
                    self._error("web_search", content)
            elif kind == "web_fetch_tool_result":
                content = _get(block, "content")
                if _get(content, "type") == "web_fetch_result":
                    fetches += 1
                    url = _get(content, "url")
                    if url and url not in self.urls_read:
                        self.urls_read.append(url)
                else:
                    self._error("web_fetch", content)
        # The billed counts come from the API (usage.server_tool_use); the result blocks if it has none.
        server = _get(usage, "server_tool_use")
        self.searches += (_get(server, "web_search_requests", 0) or 0) if server is not None else searches
        self.fetches += (_get(server, "web_fetch_requests", 0) or 0) if server is not None else fetches
        self.stop = _get(response, "stop_reason")

    def _error(self, tool: str, content: Any) -> None:
        key = f"{tool}:{str(_get(content, 'error_code', 'unknown'))[:40]}"
        self.tool_errors[key] = self.tool_errors.get(key, 0) + 1

    def output_left(self) -> int:
        return self.caps.output_tokens - self.output_tokens

    def can_continue(self) -> str | None:
        """Why the next pause_turn continuation may NOT run (None: it may)."""
        caps = self.caps
        if self.requests > AGENT_MAX_CONTINUATIONS:
            return "continuations"
        if self.searches >= caps.searches or self.fetches >= caps.fetches:
            return "tool caps"  # never a max_uses of 0 (a continuation keeps the same tools)
        if self.output_left() < AGENT_MIN_OUTPUT_TOKENS:
            return "output cap"
        prompt, completion = CLAUDE_PRICES[OPUS_55]
        # The next request re-reads at least the last one's context; with the
        # rest of the searches it must still fit the reservation.
        context = self.last_input + self.output_tokens
        nxt = (context * prompt + min(CLAUDE_MAX_TOKENS, self.output_left()) * completion) / 1_000_000
        if self.input_tokens + context > caps.input_tokens \
                or self.cost + nxt + (caps.searches - self.searches) * AGENT_SEARCH_PRICE > agent_reserve(self.seasonal, self.name) + 1e-9:
            return "reservation"
        return None

    def log_entry(self) -> dict:
        """For the private question log (fall26-data): never printed."""
        reserve = agent_reserve(self.seasonal, self.name)
        return {
            "model": self.name, "outcome": self.outcome, "stop_reason": self.stop, "requests": self.requests,
            "searches": self.searches, "fetches": self.fetches, "urls_read": list(self.urls_read),
            "urls_found": list(self.urls_found), "tool_errors": dict(self.tool_errors),
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "seconds": round(self.seconds, 1), "cost": round(reserve if self.worst_case else self.cost, 6),
            "usage_cost": round(self.cost, 6), "reserve": round(reserve, 6),
        }


def agent_tools(searches: int, fetches: int) -> list[dict]:
    return [
        {"type": AGENT_SEARCH_TOOL, "name": "web_search", "max_uses": searches,
         "blocked_domains": list(AGENT_BLOCKED_DOMAINS)},
        {"type": AGENT_FETCH_TOOL, "name": "web_fetch", "max_uses": fetches,
         "max_content_tokens": AGENT_FETCH_MAX_CONTENT_TOKENS, "blocked_domains": list(AGENT_BLOCKED_DOMAINS)},
    ]


class _AgentCall(GeneralLlm):
    """The agent at the bottom of its ThrottledLlm: Opus 5.5 (effort high) with
    web_search + web_fetch and the same prompt as a plain Opus slot (question,
    criteria, fine print, dates, the research dossier and the answer format).
    pause_turn: the paused content goes back as-is (same tools, max_uses =
    what is left), at most AGENT_MAX_CONTINUATIONS times. A 7-minute cap (less
    if the planned wait has less left, keeping AGENT_FALLBACK_SECONDS for the
    fallback) cancels it. Any failure: the plain Opus chain (its backup)."""

    def _time_cap(self) -> float:
        left = seconds_left()
        cap = AGENT_TIMEOUT_SECONDS if left is None else min(AGENT_TIMEOUT_SECONDS, left - AGENT_FALLBACK_SECONDS)
        if cap < AGENT_MIN_SECONDS:
            raise NoAnswer(f"{self.model}: not enough time for the agent", cost=0.0)
        return cap

    async def _mockable_direct_call_to_model(self, prompt: Any) -> TextTokenCostResponse:
        run = AgentRun(seasonal=agent_seasonal(), name=self.model.removeprefix("replay/"))
        cap = self._time_cap()
        started = time.monotonic()
        budget = getattr(self, "_spend", None)
        try:
            return await asyncio.wait_for(self._turn(prompt, run, cap), cap)
        except asyncio.TimeoutError:
            run.outcome, run.worst_case = "timed out", True
            raise AgentTimeout(f"{self.model}: over its time cap") from None
        finally:
            run.seconds = time.monotonic() - started
            if run.outcome == "running":
                run.outcome = "failed"
            entries = agent_log.get()
            if entries is not None:
                entries.append(run.log_entry())
            if isinstance(budget, ClaudeBudget):
                budget.note_agent(current_question_key.get(), {
                    "searches": run.searches, "fetches": run.fetches, "pauses": max(0, run.requests - 1),
                    "outcome": run.outcome[:60], "stop_reason": str(run.stop)[:20], "tool_errors": dict(run.tool_errors),
                    "input_tokens": run.input_tokens, "output_tokens": run.output_tokens}, run.name)
                if run.cost > agent_reserve(run.seasonal, run.name) + 1e-9:
                    # Fail closed: an agent over its reservation switches the agent off for this run.
                    logger.warning(f"Question {current_question_key.get()}: agent over its reservation "
                                   f"(${run.cost:.4f}); agent off for this run")
                    budget.agent_off = "over its reservation"
                    budget.agent_overruns += 1

    async def _turn(self, prompt: Any, run: AgentRun, cap: float) -> TextTokenCostResponse:
        import anthropic

        caps = run.caps
        system, messages = _messages(prompt)
        note = AGENT_SYSTEM.format(searches=caps.searches, fetches=caps.fetches)
        request: dict[str, Any] = dict(
            model=OPUS_55.removeprefix("anthropic/"), output_config={"effort": CLAUDE_EFFORT},
            system=note if not system else f"{note}\n\n{system}",
        )
        async with anthropic.AsyncAnthropic(max_retries=0, timeout=cap) as client:
            while True:
                try:
                    response = await client.messages.create(
                        **request, messages=messages, max_tokens=min(CLAUDE_MAX_TOKENS, run.output_left()),
                        tools=agent_tools(caps.searches - run.searches, caps.fetches - run.fetches),
                    )
                except Exception as e:
                    if run.requests == 0:
                        raise  # the first request: charged as any Claude call (400-429, 5xx: $0)
                    # A continuation failed: the earlier requests were billed; a
                    # timeout or cut connection: the worst case, as any Claude call.
                    status = http_status(e)
                    no_charge = status is not None and status in ClaudeBudget.no_charge_statuses
                    run.worst_case = not no_charge
                    raise NoAnswer(f"{self.model}: continuation failed ({type(e).__name__})",
                                   cost=run.cost if no_charge else None) from e
                run.add(response)
                if run.stop != "pause_turn":
                    break
                why = run.can_continue()
                if why is not None:
                    run.outcome = f"paused, not continued ({why})"
                    raise NoAnswer(f"{self.model}: {run.outcome}", cost=run.cost)
                # The same turn goes on: the paused content back as-is, no new user message.
                last = messages[-1]
                if last.get("role") == "assistant":
                    messages = [*messages[:-1], {"role": "assistant", "content": [*last["content"], *response.content]}]
                else:
                    messages = [*messages, {"role": "assistant", "content": list(response.content)}]
        text = "".join(run.texts).strip()
        if run.stop == "refusal":
            run.outcome = "refused"
            raise NoAnswer(f"{self.model}: refused", cost=run.cost)
        if run.stop != "end_turn" or not text:
            run.outcome = f"no answer ({run.stop})"
            raise NoAnswer(f"{self.model}: no answer (stop reason {run.stop})", cost=run.cost)
        run.outcome = "answered"
        return ClaudeResponse(
            stop_reason=run.stop, data=text, prompt_tokens_used=run.input_tokens, completion_tokens_used=run.output_tokens,
            total_tokens_used=run.input_tokens + run.output_tokens, model=self.model, cost=run.cost,
        )


class AgentLlm(ThrottledLlm, _AgentCall):
    """The agent slot: pacing and the Claude spend guard (ThrottledLlm) around
    one agent turn; its backup is the plain Opus chain (any failure)."""


# ---------------------------------------------------------------- the planner


@dataclass
class FrontierPlanner(bot_config.GeminiPool):
    """Today's Gemini pool (free Flash, paid-Flash fallback, Nemotron backup)
    with Claude and GPT slots in front, by claude_mode."""

    claude: ClaudeBudget | None = None
    mode: str = "off"
    mode_reason: str = "not started"
    gpt_allowed: bool = False
    gpt_enabled: bool = field(default_factory=lambda: bot_config.FRONTIER_GPT_ENABLED)
    agent_enabled: bool = field(default_factory=lambda: bot_config.FRONTIER_AGENT_ENABLED)
    seasonal_agents: int = field(default_factory=lambda: bot_config.FRONTIER_SEASONAL_AGENTS)
    # The class and model-name prefix of the Claude and GPT slots (the
    # rehearsal swaps in recorded replies: ReplayChainLlm, "replay/").
    claude_llm_class: type = ClaudeLlm
    gpt_llm_class: type = ThrottledLlm
    agent_llm_class: type = AgentLlm
    model_prefix: str = ""
    claude_pacer: RequestPacer = field(default_factory=lambda: RequestPacer(CLAUDE_REQUESTS_PER_MINUTE))
    gpt_pacer: RequestPacer = field(default_factory=lambda: RequestPacer(600))
    # Calls already in the ledgers when the run started (frontier_report counts the rest).
    calls_before: dict = field(default_factory=dict)

    def start_run(self, key_left: float | None, api_key: bool | None = None) -> str:
        """Once per run, before forecasting: this run's mode and GPT."""
        self.calls_before = {
            (provider, q): len(e.get("calls", []))
            for provider, guard in (("Anthropic", self.claude), ("OpenRouter", self.spend)) if guard is not None
            for q, e in guard.questions.items()
        }
        has_key = bool(os.getenv("ANTHROPIC_API_KEY")) if api_key is None else api_key
        self.mode, self.mode_reason = claude_mode(self.claude, has_key)
        self.gpt_allowed = (
            self.gpt_enabled and self.mode != "off" and self.spend is not None and self.spend.blocked is None
            and key_left is not None and key_left >= GPT_MIN_KEY_LEFT
        )
        why = "switched off" if not self.gpt_enabled else ("frontier off" if self.mode == "off" else "key under $2 left or unknown")
        gpt = "on" if self.gpt_allowed else f"off ({why})"
        line = f"Frontier lineup: {self.mode} ({self.mode_reason}); GPT-5.6 Sol {gpt}"
        if self.agent_enabled:
            line += f"; agent {'on' if self.mode == 'full' else 'off (' + self.mode + ' mode)'}"
            if self.seasonal_agents >= 2 and self.claude is not None:
                self.claude.daily_cap = CLAUDE_DAILY_CAP_TWO_AGENTS  # the MiniBench limit stays $9
                line += "; 2 seasonal agents"
            elif self.claude is not None:
                self.claude.daily_cap = CLAUDE_DAILY_CAP
        return line

    def _claude_chain(self, model: str, deadline_aware: bool = False) -> ThrottledLlm:
        llm: ThrottledLlm | None = None
        for name in reversed([model, *CLAUDE_BACKUPS.get(model, [])]):
            llm = self.claude_llm_class(
                model=self.model_prefix + name, pacer=self.claude_pacer, backup=llm, spend=self.claude,
                temperature=None, timeout=CLAUDE_TIMEOUT_SECONDS, allowed_tries=1, spend_backups=True,
            )
            if deadline_aware:
                llm.deadline_aware = True  # the agent's fallback: only the planned wait's time left
        assert llm is not None
        return llm

    def _agent_chain(self, name: str = AGENT) -> ThrottledLlm:
        # Any agent failure (time cap, tool or request error, no answer, a cap
        # refusal): its own plain Opus chain, with the time the planned wait has left.
        return self.agent_llm_class(
            model=self.model_prefix + name, pacer=self.claude_pacer, backup=self._claude_chain(OPUS_55, deadline_aware=True),
            spend=self.claude, temperature=None, timeout=AGENT_TIMEOUT_SECONDS, allowed_tries=1,
            spend_backups=True, backup_on_any_error=True,
        )

    def _agent_on(self) -> bool:
        # Full mode only: in "low" / "minimal" the single Claude slot is plain Opus (the cheaper one).
        return self.agent_enabled and self.mode == "full" and self.claude is not None and self.claude.agent_off is None

    def referee_llm(self) -> ThrottledLlm | None:
        """The referee shadow (shadow.py): Opus 5.5, effort high, through the
        Claude budget (day, MiniBench $9, question and cycle caps); one try, no
        backups (a cap refusal = no referee). None while Claude is off or blocked."""
        if self.claude is None or not self._frontier_on():
            return None
        question = current_question_key.get()
        if question not in self.claude.caps:  # plan() sets it; fail closed to the question kind's cap
            self.claude.set_cap(question, CLAUDE_SEASONAL_QUESTION_CAP if question_seasonal.get() is True else CLAUDE_MINIBENCH_QUESTION_CAP)
        return self.claude_llm_class(
            model=self.model_prefix + OPUS_55, pacer=self.claude_pacer, backup=None, spend=self.claude,
            temperature=None, timeout=CLAUDE_TIMEOUT_SECONDS, allowed_tries=1,
        )

    def _gpt_chain(self) -> ThrottledLlm:
        # Any GPT failure: this slot goes to the Flash chain (never the reserve).
        return self.gpt_llm_class(
            model=self.model_prefix + GPT_SOL, pacer=self.gpt_pacer, backup=self.unplanned_forecaster(),
            spend=self.spend, temperature=None, timeout=GPT_TIMEOUT_SECONDS, allowed_tries=1,
            extra_body=bot_config.HIGH_REASONING, backup_on_any_error=True,
        )

    def _claude_ok(self, model: str) -> bool:
        return self.claude is not None and self.claude.refusal(self.model_prefix + model) is None

    def _todays_setup(self) -> None:
        """Paid Flash as today: until a question has 3 real forecasts."""
        if self.spend is not None:
            self.spend.forecast_target = bot_config.PAID_FLASH_TARGET_FORECASTS

    def _frontier_on(self) -> bool:
        return self.mode != "off" and not (self.claude is not None and self.claude.blocked)

    def plan(self, seasonal: bool, only_model: str | None = None, binary: bool = True, limit: int | None = None) -> list[ThrottledLlm]:
        if only_model or not self._frontier_on():
            self._todays_setup()
            return super().plan(seasonal, only_model=only_model, binary=binary, limit=limit)
        question = current_question_key.get()
        assert self.claude is not None
        self.claude.set_cap(question, CLAUDE_SEASONAL_QUESTION_CAP if seasonal else CLAUDE_MINIBENCH_QUESTION_CAP)
        slots = list(MODE_SLOTS[self.mode][0 if seasonal else 1])
        agent = self._agent_on()
        if agent:
            # The agent replaces one Opus slot; the question cap rises while it runs.
            # Its reservation follows the question kind (main.py sets the same value).
            question_seasonal.set(seasonal)
            two = seasonal and self.seasonal_agents >= 2
            self.claude.set_cap(question, AGENT2_SEASONAL_QUESTION_CAP if two else
                                AGENT_SEASONAL_QUESTION_CAP if seasonal else AGENT_MINIBENCH_QUESTION_CAP)
            agent = self._claude_ok(AGENT)
            if agent:
                # Agent + plain Opus 5.5 (+ Flash x2 below); two seasonal agents: agent2 replaces plain Opus.
                second = AGENT2 if two and self._claude_ok(AGENT2) else OPUS_55
                if two and second == OPUS_55:
                    self.claude.set_cap(question, AGENT_SEASONAL_QUESTION_CAP)
                slots = [AGENT, second]
            else:  # refused (e.g. the day cap): plain Opus slots and caps, as without the agent
                self.claude.set_cap(question, CLAUDE_SEASONAL_QUESTION_CAP if seasonal else CLAUDE_MINIBENCH_QUESTION_CAP)
        # A slot whose model a cap refuses now is left out (its backups may still fit).
        claude_models = [m for m in slots if self._claude_ok(m)]
        gpt = False
        if seasonal and self.gpt_allowed and self.spend is not None:
            self.spend.set_cap(question, OPENROUTER_SEASONAL_QUESTION_CAP)
            gpt = self.spend.refusal(self.model_prefix + GPT_SOL) is None
        if not claude_models and not gpt:
            # No Claude slot for this question: today's setup.
            self._todays_setup()
            self.last_plan = {"frontier": self.mode, "claude": [], "reason": "no Claude slot: today's setup"}
            plan = super().plan(seasonal, binary=binary)
            self.last_plan.update(frontier=self.mode, claude=[])
            return plan
        # At most 2 Flash, no top-ups; paid Flash may fill both of them (the
        # 'enough forecasts' rule would count the Claude forecasts).
        flash = super().plan(seasonal, binary=binary, limit=FLASH_FORECASTS)
        if self.spend is not None:
            self.spend.forecast_target = None
            if gpt:
                # After the pool set its $0.10: GPT + paid Flash share $0.30 per seasonal question.
                self.spend.set_cap(question, OPENROUTER_SEASONAL_QUESTION_CAP)
        claude_chains = [self._agent_chain(m) if m in AGENTS else self._claude_chain(m) for m in claude_models]
        chains = claude_chains + ([self._gpt_chain()] if gpt else []) + flash
        self.last_plan.update(frontier=self.mode, claude=claude_models, gpt=gpt, flash=len(flash), count=len(chains))
        if self.agent_enabled:
            self.last_plan["agent"] = AGENT in claude_models
        return chains

    def round2(self, seasonal: bool, used_models: list[str]) -> list[ThrottledLlm]:
        # Frontier: the planned forecasts only, no round 2 (Flash never outvotes Claude).
        if self._frontier_on():
            return []
        return super().round2(seasonal, used_models)

    def save(self) -> None:
        super().save()
        if self.claude is not None:
            self.claude.save()


def agent_overrun_alert(planner: FrontierPlanner, notify: Any, today: str | None = None) -> bool:
    """After a run: an agent call over its reservation (the agent was switched
    off for the rest of the run) opens an alert issue, once a UTC day."""
    claude = planner.claude
    if claude is None or not claude.agent_overruns:
        return False
    _alert(claude, "agent-overrun", today or utc_day(), notify, "agent call over its reservation",
           f"{claude.agent_overruns} agent call(s) this run cost more than the reservation (${agent_reserve(True):.2f} "
           f"seasonal, ${agent_reserve(False):.2f} MiniBench); the agent was switched off for the rest of the run. "
           "Each call is charged in full. Check the Frontier lineup table of the run.")
    return True


def frontier_report(planner: FrontierPlanner) -> str:
    """Per-model calls, output tokens, seconds and dollars this run (Anthropic
    and OpenRouter apart), and $ per question. Numbers only (public log)."""
    rows: dict[tuple[str, str], dict] = {}
    per_question: dict[tuple[str, str], float] = {}
    for provider, guard in (("Anthropic", planner.claude), ("OpenRouter", planner.spend)):
        if guard is None:
            continue
        for question, entry in guard.questions.items():
            for call in entry.get("calls", [])[planner.calls_before.get((provider, question), 0):]:
                # The GPT shadow (gpt_shadow.py) is counted apart from paid Flash: its own row and $.
                shadow = provider == "OpenRouter" and call.get("shadow") == "gpt"
                where = "OpenRouter (GPT shadow)" if shadow else provider
                row = rows.setdefault((where, call["model"]), {"calls": 0, "ok": 0, "tokens": [], "seconds": [], "dollars": 0.0, "failed": []})
                row["calls"] += 1
                row["ok"] += bool(call.get("ok"))
                row["dollars"] += call.get("charged", 0.0)
                if call.get("output_tokens"):
                    row["tokens"].append(call["output_tokens"])
                if call.get("seconds") is not None:
                    row["seconds"].append(call["seconds"])
                if not call.get("ok"):
                    row["failed"].append(str(call.get("http_status", "?")))
                per_question[(where, question)] = per_question.get((where, question), 0.0) + call.get("charged", 0.0)
    lines = ["| Provider | Model | Calls | Answered | Output tokens (max) | Seconds (max) | Over 6 min | $ | Failed (HTTP) |",
             "|---|---|---|---|---|---|---|---|---|"]
    for (provider, model), r in sorted(rows.items()):
        tokens = f"{sum(r['tokens']) // max(1, len(r['tokens'])):,} ({max(r['tokens']):,})" if r["tokens"] else "-"
        secs = f"{sum(r['seconds']) / len(r['seconds']):.0f} ({max(r['seconds']):.0f})" if r["seconds"] else "-"
        slow = sum(s > 360 for s in r["seconds"])
        lines.append(f"| {provider} | {model} | {r['calls']} | {r['ok']} | {tokens} | {secs} | {slow or '-'} | ${r['dollars']:.4f} | {', '.join(r['failed']) or '-'} |")
    lines += ["", "| Provider | Question | $ |", "|---|---|---|"]
    lines += [f"| {p} | {q} | ${d:.4f} |" for (p, q), d in sorted(per_question.items())]
    claude_calls = [(question, call) for question, entry in (planner.claude.questions.items() if planner.claude else [])
                    for call in entry.get("calls", [])[planner.calls_before.get(("Anthropic", question), 0):]]
    if claude_calls:
        # Each Claude call (numbers and API labels only).
        lines += ["", "| Question | Claude call | Seconds | Input tokens | Output tokens | $ | Stop reason | HTTP |",
                  "|---|---|---|---|---|---|---|---|"]
        for question, c in claude_calls:
            secs = c.get("seconds")
            lines.append(f"| {question} | {c['model'].removeprefix('anthropic/')} | "
                         f"{'-' if secs is None else f'{secs:.0f}'}{' **over 6 min**' if (secs or 0) > 360 else ''} | "
                         f"{c.get('input_tokens') or '-'} | {c.get('output_tokens') or '-'} | ${c.get('charged', 0.0):.4f} | "
                         f"{c.get('stop_reason') or '-'} | {c.get('http_status') or '-'} |")
    agent = [call for question, entry in (planner.claude.questions.items() if planner.claude else [])
             for call in entry.get("calls", [])[planner.calls_before.get(("Anthropic", question), 0):] if "searches" in call]
    if agent:
        # The agent (numbers only: no URLs, no research).
        secs = [c["seconds"] for c in agent if c.get("seconds") is not None]
        lines += ["", "| Agent calls | Answered | Searches | Fetches | Seconds (max) | $ |", "|---|---|---|---|---|---|",
                  f"| {len(agent)} | {sum(bool(c.get('ok')) for c in agent)} | {sum(c.get('searches') or 0 for c in agent)} | "
                  f"{sum(c.get('fetches') or 0 for c in agent)} | "
                  f"{(f'{sum(secs) / len(secs):.0f} ({max(secs):.0f})') if secs else '-'} | "
                  f"${sum(c.get('charged', 0.0) for c in agent):.4f} |"]
        lines += ["", "| Agent call | Seconds | Input tokens | Output tokens | $ | Searches | Fetches | Pauses | Outcome | Tool errors |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for c in agent:
            inputs = c.get("input_tokens") or 0
            errors = ", ".join(f"{k} x{v}" for k, v in (c.get("tool_errors") or {}).items()) or "-"
            lines.append(f"| {c['model'].removeprefix('anthropic/')} | {c.get('seconds', '-')} | "
                         f"{inputs}{' **over 150k**' if inputs > 150_000 else ''} | {c.get('output_tokens') or '-'} | "
                         f"${c.get('charged', 0.0):.4f} | {c.get('searches', 0)} | {c.get('fetches', 0)} | "
                         f"{c.get('pauses', 0)} | {c.get('outcome', '-')} | {errors} |")
    return "\n".join(lines)


def _frontier_pool(store, spend: SpendGuard | None, claude: ClaudeBudget | None, llm_class: type = ThrottledLlm) -> FrontierPlanner:  # type: ignore[no-untyped-def]
    base = bot_config._gemini_pool(store, llm_class=llm_class)
    planner = FrontierPlanner(
        ledger=base.ledger, pacers=base.pacers, nemotron=base.nemotron, llm_class=llm_class, claude=claude,
    )
    if spend is not None:
        bot_config.enable_paid_flash(planner, spend, llm_class=llm_class)
    return planner
