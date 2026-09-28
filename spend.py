"""
Credits readiness, 4d (architect, 29 Sep): spend guards for the credits
lineup (OFF until the architect switches it on).

- SpendGuard: every paid OpenRouter call's real cost (OpenRouter usage
  accounting, passed through by LiteLLM as the call's cost) goes into a spend
  ledger in fall26-data (status/spend.json): per UTC day and per question.
  - Per-question hard cap = 2x the tier's cost per question: a call that could
    take the question over it is refused (SpendCapReached), so no new
    forecasts start; the finished ones are combined (median) as usual.
    Each call reserves an estimate (real prices, ~7,500 prompt tokens, an
    ASSUMED 8,000 output tokens) while it runs, so calls running at the same
    time can't overshoot together.
  - A call that times out may still be billed: it is charged the estimate,
    and that paid model is never tried again for the question (the backup
    gets it).
  - The daily total feeds the daily cap (2x the target daily spend: main.py
    then runs Lean for the rest of the day and opens an alert issue).
- FinishedForecasts: model forecasts already finished for a question
  (status/finished_forecasts.json) are reused when the question is retried by
  a later run: never bought twice.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import ensemble
from bot_helpers import PUBLIC_LOGGER_NAME
from gemini_budget import current_question_key

logger = logging.getLogger(PUBLIC_LOGGER_NAME)

SPEND_PATH = "status/spend.json"
FINISHED_PATH = "status/finished_forecasts.json"
QUESTION_CAP_FACTOR = 2.0
DAILY_CAP_FACTOR = 2.0
KEEP_DAYS = 7
FINISHED_KEEP = timedelta(days=3)
# Estimate for one paid forecast (reservation, and the charge for a timeout).
ESTIMATE_PROMPT_TOKENS = 7500
ESTIMATE_OUTPUT_TOKENS = 8000  # ASSUMED, as in the 4b cost table


class SpendCapReached(RuntimeError):
    """This question's spend cap would be passed: no new paid forecast."""


def estimate_cost(model: str) -> float:
    prompt_per_m, completion_per_m = ensemble.MODEL_PRICES.get(
        model.removeprefix("replay/"), ensemble.UNKNOWN_MODEL_PRICE
    )
    return (ESTIMATE_PROMPT_TOKENS * prompt_per_m + ESTIMATE_OUTPUT_TOKENS * completion_per_m) / 1_000_000


def utc_day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).date().isoformat()


class SpendGuard:
    def __init__(self, store: Any) -> None:
        self._store = store
        try:
            data = store.load() or {}
        except Exception as e:
            logger.warning(f"Spend ledger could not be loaded ({type(e).__name__}); starting from 0")
            data = {}
        self.days: dict[str, float] = {k: float(v) for k, v in data.get("days", {}).items()}
        self.questions: dict[str, dict] = dict(data.get("questions", {}))
        self.alerted: str | None = data.get("alerted")
        self.caps: dict[str, float] = {}
        self.in_flight: dict[str, float] = {}
        self.timed_out: set[tuple[str, str]] = set()
        self.paid_calls = 0
        self.refused = 0

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
        """'cap' (stop: no new paid forecast for this question), 'timed out'
        (this model timed out on this question: go to the backup) or None."""
        question = current_question_key.get()
        if question is None:
            return None
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

    def finish(self, model: str, cost: float | None, timed_out: bool = False) -> None:
        """cost: the real cost of a successful call (None = unknown). A call
        that timed out or was cut off is charged the estimate."""
        question = current_question_key.get()
        estimate = estimate_cost(model)
        if question is not None:
            self.in_flight[question] = max(0.0, self.in_flight.get(question, 0.0) - estimate)
        if timed_out:
            charge = estimate
            if question is not None:
                self.timed_out.add((question, model))
        else:
            charge = float(cost or 0.0)
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
            "alerted": self.alerted,
        }

    def save(self) -> None:
        try:
            self._store.save(self.snapshot())
        except Exception as e:
            logger.warning(f"Spend ledger could not be saved ({type(e).__name__})")
            print("::warning title=spend-ledger-failed::Spend ledger could not be saved")


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
