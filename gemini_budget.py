"""
Daily request budget for the free Gemini key.

The free tier allows a fixed number of requests per model per day (the day
resets at midnight Pacific, 07:00 UTC in summer time). Runs are separate
GitHub Actions jobs, so the day's counts are kept in a small JSON file in the
private fall26-data repo. The forecasting workflows share one concurrency
group, so only one run updates it at a time.

Only successful requests are counted (architect, 27 Sep 2026): "overloaded"
(503) and other failed calls don't count. A model is used up for the day only
when Google itself answers "quota exceeded" (429 RESOURCE_EXHAUSTED).
"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

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


class GitHubFileStore:
    """Keeps the ledger as a JSON file in a GitHub repo (the private fall26-data)."""

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
        response = requests.put(
            self._url, headers=self._headers, json=payload, timeout=30
        )
        response.raise_for_status()
        self._sha = response.json()["content"]["sha"]


class QuotaLedger:
    """
    Counts requests per model for the current quota day.

    - usable: requests per model we plan with (daily limit minus the reserve)
    - reserve: the rest, used only when a question would otherwise get no
      forecast at all
    Models "booked" for a planned forecast are held in `pending` until the
    call starts, and calls in progress are held in `in_flight` until they end,
    so questions forecast at the same time don't double-book.
    """

    def __init__(
        self,
        store: MemoryStore | GitHubFileStore,
        daily_limits: dict[str, int],
        reserve_fraction: float,
    ) -> None:
        self._store = store
        self.daily_limits = daily_limits
        self.reserve_fraction = reserve_fraction
        self.day = quota_day()
        self.used: dict[str, int] = {}
        self.pending: dict[str, int] = {}
        self.in_flight: dict[str, int] = {}
        try:
            data = store.load()
        except Exception as e:
            logger.warning(f"Quota ledger could not be loaded ({type(e).__name__}); starting from 0")
            data = None
        if data and data.get("day") == self.day:
            self.used = {k: int(v) for k, v in data.get("used", {}).items()}

    def _roll_over_if_new_day(self) -> None:
        today = quota_day()
        if today != self.day:
            self.day, self.used, self.pending = today, {}, {}

    def _taken(self, model: str) -> int:
        return (
            self.used.get(model, 0)
            + self.pending.get(model, 0)
            + self.in_flight.get(model, 0)
        )

    def usable_left(self, model: str) -> int:
        self._roll_over_if_new_day()
        usable = math.floor(self.daily_limits[model] * (1 - self.reserve_fraction))
        return usable - self._taken(model)

    def total_left(self, model: str) -> int:
        self._roll_over_if_new_day()
        return self.daily_limits[model] - self._taken(model)

    def book(self, model: str) -> None:
        self.pending[model] = self.pending.get(model, 0) + 1

    def start(self, model: str, booked: bool, allow_reserve: bool) -> bool:
        """Hold one request to `model` while it runs, if the budget allows it."""
        self._roll_over_if_new_day()
        if booked and self.pending.get(model, 0) > 0:
            self.pending[model] -= 1
        else:
            left = self.total_left(model) if allow_reserve else self.usable_left(model)
            if left <= 0:
                return False
        self.in_flight[model] = self.in_flight.get(model, 0) + 1
        return True

    def finish(self, model: str, succeeded: bool) -> None:
        """A held request ended: only a success counts against the quota."""
        self.in_flight[model] = max(0, self.in_flight.get(model, 0) - 1)
        if succeeded:
            self.used[model] = self.used.get(model, 0) + 1

    def mark_used_up(self, model: str) -> None:
        self.used[model] = self.daily_limits[model]

    def snapshot(self) -> dict:
        return {"day": self.day, "used": dict(sorted(self.used.items()))}

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
