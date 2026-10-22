"""
GPT shadow (architect, 22 Oct; built OFF: bot_config.GPT_SHADOW_ENABLED).

A SHADOW forecast: saved in the private '<log>_shadows.json' only, NEVER
submitted, never changes the submitted forecast. At the end of the run, beside
the free shadow forecaster and the referee (main._finish_run), one call per
live question (seasonal and MiniBench; binary, numeric, discrete, multiple
choice) to OpenRouter `openai/gpt-6.1-sol` with:
- the main forecasters' prompt for the question type, on the question's own
  research dossier (no new research of ours), and the same answer reading
  (read directly; the parser model is never called for a shadow);
- OpenRouter's NATIVE web search (plugins: [{"id": "web", "engine": "native"}]).
  OpenRouter documents NO hard limit on the number of native searches for
  OpenAI models (the server tool's `max_uses` is forwarded to Anthropic only;
  `max_results` is ignored by native search). Closest documented controls,
  all sent: the top-level `max_tool_calls: 3` (server-tool step budget), low
  search context, and a system note "at most 3 searches". The searches
  OpenRouter reports (usage.server_tool_use.web_search_requests) are saved;
  the reservation covers 3, and a call that costs more than its reservation
  is charged in full and switches the GPT shadow off for the rest of the run.

Money (OpenRouter, the SAME ledger as paid Flash: fall26-data status/spend.json,
so key usage = key_day_start + days still holds): each call reserves its worst
case first (RESERVATION, $0.25), fail closed:
- $0.25 per question (GPT shadow spend only), $1.50 per UTC day (GPT shadow
  spend only, kept apart in `gpt_shadow_days`; paid Flash's own caps leave it
  out); seasonal questions are reserved first, and a MiniBench (or unknown)
  question only while the day's GPT shadow spend, running reservations
  included, stays under $1.00 with this call (so $0.50 stays for seasonal);
- no GPT shadow when the OpenRouter key has under $2 left (or unknown), when
  the spend ledger can't be read, or the spend guard is blocked;
- one try: no retries, no backups; a 402 (credit) is never retried and
  switches the GPT shadow off for the rest of the run;
- charged: OpenRouter's reported cost; the reservation when the cost is unknown,
  for a timeout / cancel, or a failure after it may have been billed; $0 when
  rejected before generating (HTTP 400-404, 429).
Public log: "Question N: GPT shadow <status>, $cost" only.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from forecasting_tools import GeneralLlm

import bot_config
from bot_helpers import PUBLIC_LOGGER_NAME
from spend import PRE_GENERATION_REJECTIONS, SpendCapReached, SpendGuard, error_details, http_status, utc_day

logger = logging.getLogger(PUBLIC_LOGGER_NAME)

MODEL = bot_config.GPT_SHADOW_MODEL  # openrouter/openai/gpt-6.1-sol
LEDGER_MODEL = MODEL.removeprefix("openrouter/")  # openai/gpt-6.1-sol, as the spend ledger names models
VARIANT = "gpt-shadow"  # its name in the shadows file's "shadow" variants
PRICE_INPUT, PRICE_OUTPUT = 2.00, 10.00  # dollars per million tokens (OpenRouter, 22 Oct)
SEARCH_PRICE = 0.01  # dollars per native search call (OpenAI, passed through by OpenRouter)
MAX_SEARCHES = 3
# Reserved per call (worst case we allow for): the dossier prompt (~7.5k
# tokens) plus the search text OpenAI bills as input tokens, ASSUMED at most
# 60k in all; output (reasoning included) capped at 10k tokens; 3 searches.
RESERVE_INPUT_TOKENS = 60_000
MAX_OUTPUT_TOKENS = 10_000
RESERVATION = round(
    (RESERVE_INPUT_TOKENS * PRICE_INPUT + MAX_OUTPUT_TOKENS * PRICE_OUTPUT) / 1_000_000 + MAX_SEARCHES * SEARCH_PRICE, 6
)  # 0.12 + 0.10 + 0.03 = $0.25
TIMEOUT_SECONDS = 240  # then cancelled and charged the reservation (also capped by the run's minute 47)
# Reasoning is mandatory for this model; medium keeps it inside the 10k output tokens.
REASONING = {"effort": "medium"}
SEARCH_NOTE = (
    f"You may search the web, at most {MAX_SEARCHES} searches in all. Do not use metaculus.com. "
    "Use the research given below first."
)
METACULUS_DOMAIN = "metaculus.com"

# Statuses ("gpt_shadow" -> "status" in the private shadows file).
SAVED = "saved"
NO_ANSWER = "no answer"
TIMED_OUT = "timed out"
OFF = "skipped: switched off"
DAY_CAP = "skipped: day cap"
QUESTION_CAP = "skipped: question cap"
MINIBENCH_PRIORITY = "skipped: MiniBench under seasonal priority"
KEY_LOW = "skipped: key under $2"
KEY_UNKNOWN = "skipped: key unknown"
LEDGER = "skipped: spend ledger unreadable"
BLOCKED = "skipped: spend guard blocked"
TIME = "skipped: time"
NO_RESEARCH = "skipped: no research"


def request_extras() -> dict:
    """The OpenRouter-only request fields (sent as the body's top level)."""
    return {
        "reasoning": dict(REASONING),
        "plugins": [{"id": "web", "engine": "native", "max_results": 5}],
        "max_tool_calls": MAX_SEARCHES,
        "web_search_options": {"search_context_size": "low"},
    }


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def usage_numbers(usage: Any) -> dict:
    """Searches, input / output tokens and OpenRouter's cost from a response's
    usage (dict or object); a missing number is None."""
    searches = _get(_get(usage, "server_tool_use"), "web_search_requests")
    out = {
        "searches": int(searches) if isinstance(searches, (int, float)) else None,
        "input_tokens": _get(usage, "prompt_tokens"),
        "output_tokens": _get(usage, "completion_tokens"),
        "cost": _get(usage, "cost"),
    }
    return {k: (v if isinstance(v, (int, float)) and not isinstance(v, bool) else None) for k, v in out.items()}


def _domain(url: Any) -> str | None:
    from urllib.parse import urlparse

    try:
        host = urlparse(str(url).strip()).hostname
    except ValueError:
        return None
    host = (host or "").lower().rstrip(".")
    return host.removeprefix("www.") or None


def cited_domains(raw: Any) -> list[str] | None:
    """The DOMAINS of the URLs the reply cites (OpenRouter web search:
    choices[0].message.annotations[] = {"type": "url_citation", "url_citation":
    {"url", "title", "content", ...}}, which LiteLLM passes through on its
    Message). Sorted, unique; never the URLs or text. None: no response."""
    choices = _get(raw, "choices")
    if not choices:
        return None
    try:
        annotations = _get(_get(choices[0], "message"), "annotations") or []
        found = {_domain(_get(_get(a, "url_citation"), "url")) for a in annotations if _get(a, "type") in (None, "url_citation")}
    except Exception:  # an unexpected shape: no domains rather than a failed shadow
        return []
    return sorted(d for d in found if d)


def citation_numbers(raw: Any) -> dict:
    """domains (sorted unique), domains_count, metaculus_cited, for the private shadows file."""
    domains = cited_domains(raw)
    if domains is None:
        return {}
    return {
        "domains": domains, "domains_count": len(domains),
        "metaculus_cited": any(d == METACULUS_DOMAIN or d.endswith("." + METACULUS_DOMAIN) for d in domains),
    }


def estimate_searches(cost: float | None, input_tokens: int | None, output_tokens: int | None) -> int | None:
    """When OpenRouter doesn't report the searches (22 Oct Test Bot: usage had no
    server_tool_use): round((cost - token cost) / $0.01), at least 0; None if a
    number is missing. Private log only."""
    if not cost or cost <= 0 or input_tokens is None or output_tokens is None:
        return None
    tokens = (input_tokens * PRICE_INPUT + output_tokens * PRICE_OUTPUT) / 1_000_000
    return max(0, round((cost - tokens) / SEARCH_PRICE))


@dataclass
class Reservation:
    """One question's reserved worst case, settled once (charged) or released (no call made)."""

    shadow: "GptShadow"
    key: str
    seasonal: bool
    amount: float = RESERVATION
    settled: bool = False
    charged: float = 0.0
    call: dict = field(default_factory=dict)

    def release(self) -> None:
        if not self.settled:
            self.settled = True
            self.shadow._unreserve(self)

    def settle(self, ok: bool, cost: float | None = None, error: BaseException | None = None,
               seconds: float | None = None, searches: int | None = None,
               input_tokens: int | None = None, output_tokens: int | None = None) -> float:
        if self.settled:
            return self.charged
        self.settled = True
        return self.shadow._charge(self, ok, cost, error, seconds, searches, input_tokens, output_tokens)


class GptShadow:
    """The run's GPT shadow: caps, reservations and the spend ledger entries."""

    def __init__(self, spend: SpendGuard | None, key_left: float | None, enabled: bool | None = None,
                 llm_class: type | None = None) -> None:
        self.spend = spend
        self.key_left = key_left
        self.enabled = bot_config.GPT_SHADOW_ENABLED if enabled is None else enabled
        self.llm_class = llm_class or GptShadowLlm
        self.day_spent_at_start = spend.day_spent() if spend is not None else 0.0
        self.off: str | None = None  # set during the run: a credit error, or a call over its reservation
        self.overruns = 0
        self.unknown_cost_calls = 0
        self.statuses: dict[str, int] = {}
        self.dollars = 0.0
        self.searches = 0

    # ------------------------------------------------------------ deciding

    def _run_committed(self) -> float:
        """This run's OpenRouter spend so far (every model) + running GPT shadow reservations."""
        assert self.spend is not None
        return max(0.0, self.spend.day_spent() - self.day_spent_at_start) + sum(self.spend.gpt_shadow_in_flight.values())

    def reserve(self, key: str, seasonal: bool | None) -> Reservation | str:
        """A reservation, or why there is no GPT shadow for this question (a
        'skipped: ...' status). Call in run order: seasonal questions first."""
        if not self.enabled:
            return OFF
        if self.off:
            return f"skipped: {self.off}"
        spend = self.spend
        if spend is None or spend.load_failed:
            return LEDGER
        if spend.blocked:
            return BLOCKED
        if self.key_left is None:
            return KEY_UNKNOWN
        if self.key_left - self._run_committed() - RESERVATION < bot_config.GPT_SHADOW_MIN_KEY_LEFT - 1e-9:
            return KEY_LOW
        in_flight = spend.gpt_shadow_in_flight
        if spend.gpt_shadow_question_spent(key) + in_flight.get(key, 0.0) + RESERVATION > bot_config.GPT_SHADOW_QUESTION_CAP + 1e-9:
            return QUESTION_CAP
        day = spend.gpt_shadow_day_spent() + sum(in_flight.values()) + RESERVATION
        if day > bot_config.GPT_SHADOW_DAILY_CAP + 1e-9:
            return DAY_CAP
        if seasonal is not True and day > bot_config.GPT_SHADOW_MINIBENCH_DAILY_CAP + 1e-9:
            # MiniBench (or unknown: fail closed) keeps $0.50 of the day for seasonal.
            return MINIBENCH_PRIORITY
        in_flight[key] = in_flight.get(key, 0.0) + RESERVATION
        return Reservation(self, key, seasonal is True)

    def stop_reason(self) -> str | None:
        return self.off

    def make_llm(self, reservation: Reservation, timeout: float) -> "GptShadowLlm":
        return self.llm_class(self, reservation, timeout)

    # ------------------------------------------------------------ charging

    def _unreserve(self, reservation: Reservation) -> None:
        assert self.spend is not None
        in_flight = self.spend.gpt_shadow_in_flight
        in_flight[reservation.key] = max(0.0, in_flight.get(reservation.key, 0.0) - reservation.amount)

    def _charge(self, reservation: Reservation, ok: bool, cost: float | None, error: BaseException | None,
                seconds: float | None, searches: int | None, input_tokens: int | None, output_tokens: int | None) -> float:
        spend = self.spend
        assert spend is not None
        self._unreserve(reservation)
        if ok:
            if cost is None or cost <= 0:
                charge = reservation.amount  # fail closed: unknown cost -> the reservation
                self.unknown_cost_calls += 1
                spend.unknown_cost_calls += 1
                logger.warning("GPT shadow: cost unknown, charged the reservation")
            else:
                charge = float(cost)
        elif http_status(error) in PRE_GENERATION_REJECTIONS:
            charge = 0.0
        else:
            charge = reservation.amount  # timeout, cancel, cut connection, failure after billing
        if http_status(error) == 402:
            self.off = "credit error"  # never retried; no more GPT shadow calls this run
        if charge > reservation.amount + 1e-9:
            self.overruns += 1
            self.off = "over its reservation"
            logger.warning(f"GPT shadow over its reservation (${charge:.4f}): off for the rest of the run")
        day = utc_day()
        spend.days[day] = spend.days.get(day, 0.0) + charge
        spend.gpt_shadow_days[day] = spend.gpt_shadow_days.get(day, 0.0) + charge
        entry = spend.questions.setdefault(reservation.key, {"spent": 0.0})
        entry["gpt_shadow"] = float(entry.get("gpt_shadow", 0.0)) + charge
        entry["last"] = day
        call = {
            # "day": the GPT shadow's day total can be rebuilt from its calls (spend.gpt_shadow_day_spent).
            "model": LEDGER_MODEL, "shadow": "gpt", "day": day, "ok": ok, "charged": round(charge, 6),
            "cost_known": bool(ok and cost), "output_tokens": output_tokens, "input_tokens": input_tokens,
            "searches": searches, "reserve": reservation.amount,
        }
        if seconds is not None:
            call["seconds"] = round(seconds, 1)
        if not ok:
            call.update(error_details(error))
        entry.setdefault("calls", []).append(call)
        reservation.charged, reservation.call = charge, call
        self.dollars += charge
        self.searches += searches or 0
        return charge

    def count(self, status: str) -> None:
        label = status.split(" (")[0]
        self.statuses[label] = self.statuses.get(label, 0) + 1

    def summary(self) -> str:
        """Numbers only (public log / step summary)."""
        parts = ", ".join(f"{k} {v}" for k, v in sorted(self.statuses.items())) or "none"
        return (f"GPT shadow ({LEDGER_MODEL}, never submitted): {parts}; ${self.dollars:.4f}; "
                f"searches {self.searches}; over reservation {self.overruns}")


def overrun_alert(shadow: GptShadow, notify: Any, today: str | None = None) -> bool:
    """After a run: a GPT shadow call over its reservation (the GPT shadow was
    switched off for the rest of the run) opens an alert issue, once a UTC day."""
    from spend import _alert

    if not shadow.overruns or shadow.spend is None:
        return False
    _alert(shadow.spend, "gpt-shadow-overrun", today or utc_day(), notify, "GPT shadow call over its reservation",
           f"{shadow.overruns} GPT shadow call(s) this run cost more than the ${RESERVATION:.2f} reservation (more "
           "searches or search text than allowed for); each was charged in full and the GPT shadow was switched "
           "off for the rest of the run. Check the GPT shadow line of the run.")
    return True


class GptShadowLlm(GeneralLlm):
    """One guarded OpenRouter call (no retries, no backup) for one question's GPT shadow."""

    def __init__(self, shadow: GptShadow, reservation: Reservation, timeout: float) -> None:
        super().__init__(
            model=MODEL, temperature=None, timeout=timeout, allowed_tries=1,
            max_tokens=MAX_OUTPUT_TOKENS, extra_body=request_extras(),
        )
        if self.model != bot_config.GPT_SHADOW_MODEL:  # this exact model only
            raise ValueError("GPT shadow: wrong model")
        self._shadow = shadow
        self._reservation = reservation
        self._raw: Any = None
        self.calls = 0
        self.numbers: dict = {}

    async def invoke(self, prompt: Any, system_prompt: str | None = None) -> str:
        if system_prompt is None and isinstance(prompt, str):
            system_prompt = SEARCH_NOTE
        return await super().invoke(prompt, system_prompt)

    async def _call_litellm(self, prompt: Any) -> Any:
        response = await super()._call_litellm(prompt)
        self._raw = response  # for the usage numbers (searches, OpenRouter's cost)
        return response

    async def _raw_call(self, prompt: Any) -> tuple[Any, Any]:
        """(TextTokenCostResponse, the response's usage). Tests replace this."""
        response = await GeneralLlm._mockable_direct_call_to_model(self, prompt)
        return response, _get(self._raw, "usage")

    async def _mockable_direct_call_to_model(self, prompt: Any) -> Any:
        if self.calls:
            raise RuntimeError("GPT shadow: one call per question, no retries")
        self.calls += 1
        why = self._shadow.stop_reason()
        if why:
            self._reservation.release()
            raise SpendCapReached(f"GPT shadow: {why}")
        started = time.monotonic()
        response, usage, failure = None, None, None
        try:
            response, usage = await self._raw_call(prompt)
            return response
        except BaseException as e:  # incl. cancellation (a timeout): it may still be billed
            failure = e
            raise
        finally:
            numbers = usage_numbers(usage)
            cost = getattr(response, "cost", None) if response is not None else None
            if not cost:
                cost = numbers["cost"]
            if response is not None:
                numbers["input_tokens"] = numbers["input_tokens"] or getattr(response, "prompt_tokens_used", None)
                numbers["output_tokens"] = numbers["output_tokens"] or getattr(response, "completion_tokens_used", None)
            self._reservation.settle(
                ok=response is not None, cost=cost, error=failure, seconds=time.monotonic() - started,
                searches=numbers["searches"], input_tokens=numbers["input_tokens"], output_tokens=numbers["output_tokens"],
            )
            if numbers["searches"] is None and response is not None:
                numbers["searches_estimated"] = estimate_searches(cost, numbers["input_tokens"], numbers["output_tokens"])
            if response is not None:
                numbers.update(citation_numbers(self._raw))
            self.numbers = numbers
