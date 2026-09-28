"""
Daily request budget for the free Gemini key.

The free tier allows a fixed number of requests per model per day (the day
resets at midnight Pacific, 07:00 UTC in summer time): 20 for the Flash
models, 500 for the Flash-Lite ones (AI Studio rate-limit page, 28 Sep 2026).
Models that share one quota count against one "bucket" (3.1-flash-lite-preview
books against 3.1-flash-lite's). Runs are separate
GitHub Actions jobs, so the day's counts are kept in a small JSON file in the
private fall26-data repo. The forecasting workflows share one concurrency
group, so only one run updates it at a time.

Every attempt is counted, failed ones included (28 Sep 2026: Google counted
~20 failed 503 attempts per Flash model against the 20/day, then answered 429).
A model is also used up for the day when Google answers "quota exceeded" (429
RESOURCE_EXHAUSTED). A model that fails twice in a run is skipped for the
rest of the run, and each question may try a model at most 4 times a day.
"""
from __future__ import annotations

import base64
import contextvars
import json
import logging
import math
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import time

import requests

from bot_helpers import PUBLIC_LOGGER_NAME

logger = logging.getLogger(PUBLIC_LOGGER_NAME)

_QUOTA_TIMEZONE = ZoneInfo("America/Los_Angeles")


def quota_day(now: datetime | None = None) -> str:
    """The Google quota day: the date in Pacific time."""
    return (now or datetime.now(_QUOTA_TIMEZONE)).astimezone(_QUOTA_TIMEZONE).date().isoformat()


class MemoryStore:
    """Keeps the ledger in memory only (local tests, or no data-repo token)."""

    def __init__(self, data: dict | None = None) -> None:
        self.data = data

    def load(self) -> dict | None:
        return self.data

    def save(self, data: dict) -> None:
        self.data = data


# Test runs without the Gemini key have their own concurrency group, so they
# may commit to fall26-data at the same moment as a live run: GitHub then
# answers 409 (the branch moved) or briefly 5xx. The same save is tried again.
# (Not 422: that means the file itself changed, which must not be overwritten.)
STORE_SAVE_ATTEMPTS = 4
STORE_RETRY_STATUSES = (409, 500, 502, 503, 504)


class GitHubFileStore:
    """Keeps the ledger as a JSON file in a GitHub repo (the private fall26-data)."""

    pause = staticmethod(time.sleep)

    def __init__(self, repo: str, path: str, token: str) -> None:
        self._url = f"https://api.github.com/repos/{repo}/contents/{path}"
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        self._sha: str | None = None

    def load(self) -> dict | None:
        response = requests.get(self._url, headers=self._headers, timeout=30)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        body = response.json()
        self._sha = body["sha"]
        return json.loads(base64.b64decode(body["content"]))

    def save(self, data: Any, message: str | None = None) -> None:
        if message is None:
            message = (
                f"Gemini quota ledger {data.get('day')}"
                if isinstance(data, dict)
                else f"Update {self._url.split('/contents/')[-1]}"
            )
        payload = {
            "message": message,
            "content": base64.b64encode(
                json.dumps(data, indent=2, sort_keys=True).encode()
            ).decode(),
        }
        if self._sha:
            payload["sha"] = self._sha
        for attempt in range(STORE_SAVE_ATTEMPTS):
            response = requests.put(
                self._url, headers=self._headers, json=payload, timeout=30
            )
            if response.status_code not in STORE_RETRY_STATUSES or attempt == STORE_SAVE_ATTEMPTS - 1:
                break
            self.pause(1.0 + attempt)
        response.raise_for_status()
        self._sha = response.json()["content"]["sha"]


# Google counts EVERY attempt against the 20/day, failed ones included: on
# 28 Sep 2026 gemini-3.6/3.7/3.8-flash each answered ~20 times "overloaded"
# (503) with 0 successes, then 429 "daily quota". So the ledger counts
# attempts, a model is skipped for the rest of a run after 2 failures, and a
# question may try each model at most a few times a day.
MAX_FAILURES_PER_MODEL_PER_RUN = 2
MAX_ATTEMPTS_PER_MODEL_PER_QUESTION_PER_DAY = 4

# The question whose calls are running in the current asyncio task (main.py).
current_question_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "quota_question", default=None
)


class QuotaLedger:
    """
    Counts requests per model for the current quota day.

    - usable: requests per model we plan with (daily limit minus the reserve)
    - reserve: the rest, used only when a question would otherwise get no
      forecast at all
    - buckets: model -> the quota bucket it counts against (default: itself);
      daily_limits are per bucket
    Models "booked" for a planned forecast are held in `pending` until the
    call starts, and calls in progress are held in `in_flight` until they end,
    so questions forecast at the same time don't double-book.
    """

    def __init__(
        self,
        store: MemoryStore | GitHubFileStore,
        daily_limits: dict[str, int],
        reserve_fraction: float,
        buckets: dict[str, str] | None = None,
    ) -> None:
        self._store = store
        self.daily_limits = daily_limits
        self.buckets = dict(buckets or {})
        self.reserve_fraction = reserve_fraction
        self.day = quota_day()
        self.used: dict[str, int] = {}
        self.pending: dict[str, int] = {}
        self.in_flight: dict[str, int] = {}
        # "post|model" -> attempts today (saved); model -> failures this run.
        self.question_attempts: dict[str, int] = {}
        self.run_failures: dict[str, int] = {}
        self.last_refusal: dict[str, str] = {}
        try:
            data = store.load()
        except Exception as e:
            logger.warning(f"Quota ledger could not be loaded ({type(e).__name__}); starting from 0")
            data = None
        if data and data.get("day") == self.day:
            for k, v in data.get("used", {}).items():
                # (A ledger saved before buckets may list a shared model on its own.)
                bucket = self.bucket(k)
                self.used[bucket] = self.used.get(bucket, 0) + int(v)
            self.question_attempts = {k: int(v) for k, v in data.get("question_attempts", {}).items()}

    def _roll_over_if_new_day(self) -> None:
        today = quota_day()
        if today != self.day:
            self.day, self.used, self.pending, self.question_attempts = today, {}, {}, {}

    def bucket(self, model: str) -> str:
        """The quota bucket `model` counts against."""
        return self.buckets.get(model, model)

    def _taken(self, model: str) -> int:
        model = self.bucket(model)
        return (
            self.used.get(model, 0)
            + self.pending.get(model, 0)
            + self.in_flight.get(model, 0)
        )

    def usable_left(self, model: str) -> int:
        self._roll_over_if_new_day()
        usable = math.floor(self.daily_limits[self.bucket(model)] * (1 - self.reserve_fraction))
        return usable - self._taken(model)

    def total_left(self, model: str) -> int:
        self._roll_over_if_new_day()
        return self.daily_limits[self.bucket(model)] - self._taken(model)

    def book(self, model: str) -> None:
        model = self.bucket(model)
        self.pending[model] = self.pending.get(model, 0) + 1

    def _question_key(self, model: str) -> str | None:
        question = current_question_key.get()
        return f"{question}|{model}" if question else None

    def _refusal(self, model: str, booked: bool, allow_reserve: bool) -> str | None:
        if self.run_failures.get(model, 0) >= MAX_FAILURES_PER_MODEL_PER_RUN:
            return f"failed {MAX_FAILURES_PER_MODEL_PER_RUN} times this run"
        key = self._question_key(model)
        if key and self.question_attempts.get(key, 0) >= MAX_ATTEMPTS_PER_MODEL_PER_QUESTION_PER_DAY:
            return f"{MAX_ATTEMPTS_PER_MODEL_PER_QUESTION_PER_DAY} attempts on this question today"
        if not booked:
            left = self.total_left(model) if allow_reserve else self.usable_left(model)
            if left <= 0:
                return "no budget left today"
        return None

    def start(self, model: str, booked: bool, allow_reserve: bool) -> bool:
        """Hold one request to `model` while it runs, if the budget and the
        retry limits allow it. A booking is released either way."""
        self._roll_over_if_new_day()
        bucket = self.bucket(model)
        booked = booked and self.pending.get(bucket, 0) > 0
        if booked:
            self.pending[bucket] -= 1
        reason = self._refusal(model, booked, allow_reserve)
        if reason:
            self.last_refusal[model] = reason
            return False
        self.in_flight[bucket] = self.in_flight.get(bucket, 0) + 1
        key = self._question_key(model)
        if key:
            self.question_attempts[key] = self.question_attempts.get(key, 0) + 1
        return True

    def finish(self, model: str, succeeded: bool) -> None:
        """A held request ended: every attempt counts against the quota
        (Google counts failed ones too); a failure also counts for this run."""
        bucket = self.bucket(model)
        self.in_flight[bucket] = max(0, self.in_flight.get(bucket, 0) - 1)
        # (Capped: a 429 "daily quota" has already marked the model used up.)
        self.used[bucket] = min(self.used.get(bucket, 0) + 1, self.daily_limits.get(bucket, math.inf))
        if not succeeded:
            self.run_failures[model] = self.run_failures.get(model, 0) + 1

    def mark_used_up(self, model: str) -> None:
        bucket = self.bucket(model)
        self.used[bucket] = self.daily_limits[bucket]

    def snapshot(self) -> dict:
        return {
            "day": self.day,
            "used": dict(sorted(self.used.items())),
            "question_attempts": dict(sorted(self.question_attempts.items())),
        }

    def save(self) -> None:
        try:
            self._store.save(self.snapshot())
        except Exception as e:
            logger.warning(f"Quota ledger could not be saved ({type(e).__name__})")
            # A GitHub Actions annotation, found by the daily health check.
            print("::warning title=quota-ledger-failed::Quota ledger could not be saved")


def make_store(path: str) -> MemoryStore | GitHubFileStore:
    token = os.getenv("DATA_REPO_TOKEN")
    if not token:
        logger.warning("DATA_REPO_TOKEN not set: Gemini quota ledger kept in memory only")
        return MemoryStore()
    return GitHubFileStore("tonyelfilali-collab/fall26-data", path, token)
