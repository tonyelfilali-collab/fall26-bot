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
  CLAUDE_BUDGET_PER_CYCLE, plus the cycle reset date; ledger caps $12 per UTC
  day, $1.50 per seasonal question, $0.60 per MiniBench question. Each call
  reserves its worst case (max_tokens of output), so a cap is never passed.
- Step-down (claude_mode, once per run): under 25% of the cycle budget left ->
  seasonal Opus x1; under 10% -> seasonal Opus x1, no Claude on MiniBench; a
  credit error, an unreadable ledger or no reset date -> no Claude: today's setup.
"""
from __future__ import annotations

import calendar
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from forecasting_tools import GeneralLlm
from forecasting_tools.ai_models.ai_utils.response_types import TextTokenCostResponse

import bot_config
import ensemble
from bot_helpers import PUBLIC_LOGGER_NAME
from gemini_budget import current_question_key
from llm_throttle import NoAnswer, RequestPacer, ThrottledLlm
from spend import SpendGuard, http_status, utc_day

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
CLAUDE_DAILY_CAP = 12.00
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


def claude_reserve(model: str) -> float:
    prompt, completion = CLAUDE_PRICES[model.removeprefix("replay/")]
    return (CLAUDE_RESERVE_PROMPT_TOKENS * prompt + CLAUDE_MAX_TOKENS * completion) / 1_000_000


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
        self.estimates = {m: claude_reserve(m) for m in CLAUDE_PRICES}

    def estimate(self, model: str) -> float:
        name = model.removeprefix("replay/")
        return claude_reserve(name) if name in CLAUDE_PRICES else super().estimate(model)

    def cycle_spent(self) -> float:
        return self.cycles.get(self.cycle.isoformat(), 0.0) if self.cycle else 0.0

    def fraction_left(self) -> float:
        return (self.budget - self.cycle_spent()) / self.budget if self.budget > 0 else 0.0

    def credit_error_this_cycle(self) -> bool:
        return self.cycle is not None and self.cycle.isoformat() in self.credit_errors

    def finish(self, model: str, succeeded: bool, cost: float | None = None, error: BaseException | None = None,
               timed_out: bool = False, output_tokens: int | None = None, seconds: float | None = None) -> None:
        before = self.day_spent()
        super().finish(model, succeeded, cost=cost, error=error, timed_out=timed_out,
                       output_tokens=output_tokens, seconds=seconds)
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

    async def _mockable_direct_call_to_model(self, prompt: Any) -> TextTokenCostResponse:
        import anthropic

        system, messages = _messages(prompt)
        request: dict[str, Any] = dict(
            model=self.model.removeprefix("anthropic/"), max_tokens=CLAUDE_MAX_TOKENS,
            output_config={"effort": CLAUDE_EFFORT}, messages=messages,
        )
        if system:
            request["system"] = system
        async with anthropic.AsyncAnthropic(max_retries=0, timeout=CLAUDE_TIMEOUT_SECONDS) as client:
            response = await client.messages.create(**request)
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        usage = response.usage
        if response.stop_reason == "refusal":
            raise NoAnswer(f"{self.model}: refused", cost=claude_cost(self.model, usage))
        if response.stop_reason == "max_tokens" or not text:
            raise NoAnswer(f"{self.model}: no answer (stop reason {response.stop_reason})", cost=claude_cost(self.model, usage))
        return TextTokenCostResponse(
            data=text, prompt_tokens_used=usage.input_tokens, completion_tokens_used=usage.output_tokens,
            total_tokens_used=usage.input_tokens + usage.output_tokens, model=self.model,
            cost=claude_cost(self.model, usage),
        )


class ClaudeLlm(ThrottledLlm, _ClaudeCall):
    """A Claude slot: pacing, the Claude spend guard and backups (ThrottledLlm)
    around one Anthropic SDK call."""


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
    # The class and model-name prefix of the Claude and GPT slots (the
    # rehearsal swaps in recorded replies: ReplayChainLlm, "replay/").
    claude_llm_class: type = ClaudeLlm
    gpt_llm_class: type = ThrottledLlm
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
        return f"Frontier lineup: {self.mode} ({self.mode_reason}); GPT-5.6 Sol {gpt}"

    def _claude_chain(self, model: str) -> ThrottledLlm:
        llm: ThrottledLlm | None = None
        for name in reversed([model, *CLAUDE_BACKUPS.get(model, [])]):
            llm = self.claude_llm_class(
                model=self.model_prefix + name, pacer=self.claude_pacer, backup=llm, spend=self.claude,
                temperature=None, timeout=CLAUDE_TIMEOUT_SECONDS, allowed_tries=1, spend_backups=True,
            )
        assert llm is not None
        return llm

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
        # A slot whose model a cap refuses now is left out (its backups may still fit).
        claude_models = [m for m in MODE_SLOTS[self.mode][0 if seasonal else 1] if self._claude_ok(m)]
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
        chains = [self._claude_chain(m) for m in claude_models] + ([self._gpt_chain()] if gpt else []) + flash
        self.last_plan.update(frontier=self.mode, claude=claude_models, gpt=gpt, flash=len(flash), count=len(chains))
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
                row = rows.setdefault((provider, call["model"]), {"calls": 0, "ok": 0, "tokens": [], "seconds": [], "dollars": 0.0, "failed": []})
                row["calls"] += 1
                row["ok"] += bool(call.get("ok"))
                row["dollars"] += call.get("charged", 0.0)
                if call.get("output_tokens"):
                    row["tokens"].append(call["output_tokens"])
                if call.get("seconds") is not None:
                    row["seconds"].append(call["seconds"])
                if not call.get("ok"):
                    row["failed"].append(str(call.get("http_status", "?")))
                per_question[(provider, question)] = per_question.get((provider, question), 0.0) + call.get("charged", 0.0)
    lines = ["| Provider | Model | Calls | Answered | Output tokens (max) | Seconds (max) | Over 6 min | $ | Failed (HTTP) |",
             "|---|---|---|---|---|---|---|---|---|"]
    for (provider, model), r in sorted(rows.items()):
        tokens = f"{sum(r['tokens']) // max(1, len(r['tokens'])):,} ({max(r['tokens']):,})" if r["tokens"] else "-"
        secs = f"{sum(r['seconds']) / len(r['seconds']):.0f} ({max(r['seconds']):.0f})" if r["seconds"] else "-"
        slow = sum(s > 360 for s in r["seconds"])
        lines.append(f"| {provider} | {model} | {r['calls']} | {r['ok']} | {tokens} | {secs} | {slow or '-'} | ${r['dollars']:.4f} | {', '.join(r['failed']) or '-'} |")
    lines += ["", "| Provider | Question | $ |", "|---|---|---|"]
    lines += [f"| {p} | {q} | ${d:.4f} |" for (p, q), d in sorted(per_question.items())]
    return "\n".join(lines)


def _frontier_pool(store, spend: SpendGuard | None, claude: ClaudeBudget | None, llm_class: type = ThrottledLlm) -> FrontierPlanner:  # type: ignore[no-untyped-def]
    base = bot_config._gemini_pool(store, llm_class=llm_class)
    planner = FrontierPlanner(
        ledger=base.ledger, pacers=base.pacers, nemotron=base.nemotron, llm_class=llm_class, claude=claude,
    )
    if spend is not None:
        bot_config.enable_paid_flash(planner, spend, llm_class=llm_class)
    return planner
