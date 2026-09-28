"""
Credits readiness, 4d (architect, 29 Sep): spend guards for the credits
lineup (OFF until the architect switches it on). Everything fails CLOSED:
when in doubt, charge the estimate, refuse the call, or run Lean.

- SpendGuard: every paid OpenRouter call's cost goes into a spend ledger in
  fall26-data (status/spend.json), per UTC day and per question.
  - Real cost: OpenRouter usage accounting, passed through by LiteLLM as the
    call's cost. A successful call with no cost (LiteLLM reports 0 when it
    has none) is charged the estimate and counted as "cost unknown" (alert).
  - A failed call is charged the estimate (it may still be billed), unless
    it was clearly rejected before generating (HTTP 400/401/402/403/404/429).
    A timeout also means that paid model is not tried again on the question.
  - A paid call with no question context is refused (fail closed).
  - Per-question hard cap = 2x the tier's cost per question: a call that could
    take the question over it is refused (SpendCapReached), so no new
    forecasts start; the finished ones are combined (median) as usual. Each
    running call reserves an estimate (real prices, ~7,500 prompt tokens, an
    ASSUMED 8,000 output tokens), so parallel calls can't overshoot together.
- guard_tier (each credits run, before forecasting): Lean when
  - the spend ledger can't be loaded (this run; alert),
  - today's spend reached the daily cap (2x the target daily spend; rest of
    the UTC day; alert),
  - the key's own usage since the day's first run exceeds our ledger's day
    total by more than max(20%, $0.50) (rest of the UTC day; alert),
  - the key's usage can't be read (this run; alert).
- FinishedForecasts: model forecasts already finished for a question
  (status/finished_forecasts.json) are reused when a later run retries it:
  never bought twice.
Alerts are GitHub issues, one per kind per day (see ensemble.open_alert_issue).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import ensemble
from bot_helpers import PUBLIC_LOGGER_NAME
from gemini_budget import current_question_key

logger = logging.getLogger(PUBLIC_LOGGER_NAME)

SPEND_PATH = "status/spend.json"
FINISHED_PATH = "status/finished_forecasts.json"
QUESTION_CAP_FACTOR = 2.0
DAILY_CAP_FACTOR = 2.0
KEY_MISMATCH_FRACTION = 0.20
KEY_MISMATCH_DOLLARS = 0.50
KEEP_DAYS = 7
FINISHED_KEEP = timedelta(days=3)
# Estimate for one paid forecast (reservation, and the charge when the real
# cost is unknown or the call failed after it may have been billed).
ESTIMATE_PROMPT_TOKENS = 7500
ESTIMATE_OUTPUT_TOKENS = 8000  # ASSUMED, as in the 4b cost table
# Rejected before any generation: never billed.
PRE_GENERATION_REJECTIONS = (400, 401, 402, 403, 404, 429)

Notify = Callable[[str, str], Any]  # (title, body)


class SpendCapReached(RuntimeError):
    """This paid call is refused: the question's spend cap would be passed,
    or there is no question to charge it to."""


def estimate_cost(model: str) -> float:
    prompt_per_m, completion_per_m = ensemble.MODEL_PRICES.get(
        model.removeprefix("replay/"), ensemble.UNKNOWN_MODEL_PRICE
    )
    return (ESTIMATE_PROMPT_TOKENS * prompt_per_m + ESTIMATE_OUTPUT_TOKENS * completion_per_m) / 1_000_000


def utc_day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).date().isoformat()


def http_status(error: BaseException | None) -> int | None:
    """The HTTP status of a failed call, if the error carries one."""
    current = error
    for _ in range(4):
        if current is None:
            return None
        status = getattr(current, "status_code", None)
        if isinstance(status, int):
            return status
        current = current.__cause__ or current.__context__
    return None


class SpendGuard:
    def __init__(self, store: Any) -> None:
        self._store = store
        # Fail closed: a ledger that can't be read (or is missing) means Lean
        # for this run and an alert; it is never silently reset to 0.
        self.load_failed = False
        self.load_error = False  # True: it exists but couldn't be read (never overwrite it)
        try:
            data = store.load()
        except Exception as e:
            logger.warning(f"Spend ledger could not be loaded ({type(e).__name__})")
            data = None
            self.load_error = True
        if data is None:
            self.load_failed = True
            data = {}
        self.days: dict[str, float] = {k: float(v) for k, v in data.get("days", {}).items()}
        self.questions: dict[str, dict] = dict(data.get("questions", {}))
        self.key_day_start: dict[str, float] = {k: float(v) for k, v in data.get("key_day_start", {}).items()}
        self.lean_days: dict[str, str] = dict(data.get("lean_days", {}))  # day -> reason
        self.alerts: dict[str, str] = dict(data.get("alerts", {}))  # kind -> day alerted
        self.caps: dict[str, float] = {}
        self.in_flight: dict[str, float] = {}
        self.timed_out: set[tuple[str, str]] = set()
        self.paid_calls = 0
        self.refused = 0
        self.unknown_cost_calls = 0

    # ------------------------------------------------------------ reading

    def question_spent(self, question: str) -> float:
        return float(self.questions.get(question, {}).get("spent", 0.0))

    def day_spent(self, day: str | None = None) -> float:
        return self.days.get(day or utc_day(), 0.0)

    def set_cap(self, question: str | None, dollars: float) -> None:
        if question:
            self.caps[question] = dollars

    # ------------------------------------------------------------ one paid call

    def refusal(self, model: str) -> str | None:
        """'cap' (stop: no new paid forecast), 'no question' (stop: nothing to
        charge it to), 'timed out ...' (go to the backup) or None."""
        question = current_question_key.get()
        if question is None:
            logger.warning(f"{model}: paid call with no question context, refused")
            return "no question"
        if (question, model) in self.timed_out:
            return "timed out earlier on this question"
        cap = self.caps.get(question)
        if cap is not None:
            committed = self.question_spent(question) + self.in_flight.get(question, 0.0)
            if committed + estimate_cost(model) > cap + 1e-9:
                return "cap"
        return None

    def start(self, model: str) -> None:
        question = current_question_key.get()
        if question is not None:
            self.in_flight[question] = self.in_flight.get(question, 0.0) + estimate_cost(model)
        self.paid_calls += 1

    def finish(self, model: str, succeeded: bool, cost: float | None = None, error: BaseException | None = None,
               timed_out: bool = False) -> None:
        """Charge one call: the real cost; the estimate when the cost is
        unknown or the call failed after it may have been billed; nothing when
        it was rejected before generating."""
        question = current_question_key.get()
        estimate = estimate_cost(model)
        if question is not None:
            self.in_flight[question] = max(0.0, self.in_flight.get(question, 0.0) - estimate)
        if succeeded:
            if cost is None or cost <= 0:
                charge = estimate
                self.unknown_cost_calls += 1
                logger.warning(f"{model}: cost unknown, charged the estimate")
            else:
                charge = float(cost)
        elif http_status(error) in PRE_GENERATION_REJECTIONS:
            charge = 0.0
        else:
            charge = estimate
        if timed_out and question is not None:
            self.timed_out.add((question, model))
        day = utc_day()
        self.days[day] = self.days.get(day, 0.0) + charge
        if question is not None:
            entry = self.questions.setdefault(question, {"spent": 0.0})
            entry["spent"] = float(entry.get("spent", 0.0)) + charge
            entry["last"] = day

    # ------------------------------------------------------------ saving

    def snapshot(self) -> dict:
        keep_from = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).date().isoformat()
        return {
            "days": {d: round(v, 6) for d, v in sorted(self.days.items()) if d >= keep_from},
            "questions": {q: e for q, e in self.questions.items() if e.get("last", keep_from) >= keep_from},
            "key_day_start": {d: v for d, v in self.key_day_start.items() if d >= keep_from},
            "lean_days": {d: r for d, r in self.lean_days.items() if d >= keep_from},
            "alerts": self.alerts,
        }

    def save(self) -> None:
        if self.load_error:
            # Never overwrite a ledger we couldn't read.
            logger.warning("Spend ledger not saved: it could not be read this run")
            print("::warning title=spend-ledger-failed::Spend ledger could not be read or saved")
            return
        try:
            self._store.save(self.snapshot())
        except Exception as e:
            logger.warning(f"Spend ledger could not be saved ({type(e).__name__})")
            print("::warning title=spend-ledger-failed::Spend ledger could not be saved")


def _alert(spend: SpendGuard | None, kind: str, today: str, notify: Notify, title: str, body: str) -> None:
    """One alert issue per kind per day."""
    if spend is not None and spend.alerts.get(kind) == today:
        return
    notify(f"Spend alert ({today}): {title}", body + " (Automatic message from the bot.)")
    if spend is not None:
        spend.alerts[kind] = today


def guard_tier(tier: str, target: float, spend: SpendGuard | None, key_usage: float | None, notify: Notify,
               today: str | None = None) -> str:
    """The spending tier for this run after the fail-closed checks."""
    today = today or utc_day()
    if spend is None:
        return tier
    if spend.load_failed:
        logger.warning("Spend ledger could not be loaded: Lean tier for this run")
        _alert(None, "ledger", today, notify, "spend ledger could not be loaded",
               f"fall26-data/{SPEND_PATH} could not be read, so the bot runs the Lean tier until it can.")
        return "lean"
    daily_cap = DAILY_CAP_FACTOR * target * ensemble.QUESTIONS_PER_DAY
    if spend.day_spent(today) >= daily_cap and today not in spend.lean_days:
        spend.lean_days[today] = "daily cap"
        _alert(spend, "daily-cap", today, notify, "daily spend cap reached",
               f"OpenRouter spend today (UTC) is about ${spend.day_spent(today):.2f}, at or over the daily cap "
               f"of about ${daily_cap:.2f} (2x the target daily spend). Lean tier until midnight UTC.")
    if key_usage is None:
        # Can't check the key's own count: fail closed for this run.
        logger.warning("OpenRouter key usage could not be read: Lean tier for this run")
        _alert(spend, "key-unreadable", today, notify, "OpenRouter key usage could not be read",
               "The backstop check (key usage vs our ledger) could not run, so this run used the Lean tier.")
        return "lean"
    else:
        start = spend.key_day_start.setdefault(today, key_usage)
        key_today = key_usage - start
        ledger = spend.day_spent(today)
        if key_today - ledger > max(KEY_MISMATCH_FRACTION * ledger, KEY_MISMATCH_DOLLARS) and today not in spend.lean_days:
            spend.lean_days[today] = "key usage above ledger"
            _alert(spend, "key-mismatch", today, notify, "key usage above our spend ledger",
                   f"The OpenRouter key spent about ${key_today:.2f} since the day's first run, but our ledger "
                   f"has ${ledger:.2f}. Lean tier until midnight UTC.")
    if today in spend.lean_days:
        logger.warning(f"Lean tier for the rest of the UTC day ({spend.lean_days[today]})")
        return "lean"
    return tier


def unknown_cost_alert(spend: SpendGuard | None, notify: Notify, today: str | None = None) -> None:
    """After a run: any call whose real cost was unknown opens an alert (once a day)."""
    if spend is None or spend.unknown_cost_calls == 0:
        return
    today = today or utc_day()
    _alert(spend, "cost-unknown", today, notify, "paid calls with unknown cost",
           f"{spend.unknown_cost_calls} paid call(s) this run came back without a cost; each was charged "
           "the estimate. Check OpenRouter usage accounting.")


class FinishedForecasts:
    """Finished model forecasts per question, kept a few days so a retry run
    reuses them instead of paying again."""

    def __init__(self, store: Any) -> None:
        self._store = store
        try:
            self.data: dict[str, dict] = dict(store.load() or {})
        except Exception:
            self.data = {}

    def get(self, question: str) -> list[dict]:
        return list(self.data.get(question, {}).get("forecasts", []))

    def put(self, question: str, forecasts: list[dict]) -> None:
        self.data[question] = {"saved": datetime.now(timezone.utc).isoformat(), "forecasts": forecasts}

    def save(self) -> None:
        cutoff = datetime.now(timezone.utc) - FINISHED_KEEP
        self.data = {q: e for q, e in self.data.items() if datetime.fromisoformat(e["saved"]) >= cutoff}
        try:
            self._store.save(self.data)
        except Exception as e:
            logger.warning(f"Finished forecasts could not be saved ({type(e).__name__})")
