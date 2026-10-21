"""
One JSON file per forecast question, saved to the private fall26-data repo:
the question snapshot, the research (with its time), each forecast's models,
raw output, parsed value and timing, and the final forecast.

Saving happens after the forecast is submitted and never raises: a failure is
logged and kept for the end-of-run retry pass (retry_failed: 3 tries over
about 2 minutes); a save still failing then is marked with a GitHub Actions
warning annotation (title "question-log-failed"), which the daily health
check looks for.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from bot_helpers import PUBLIC_LOGGER_NAME

logger = logging.getLogger(PUBLIC_LOGGER_NAME)

DATA_REPO = "tonyelfilali-collab/fall26-data"
LOG_FAILURE_ANNOTATION = "question-log-failed"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


def question_snapshot(question: Any) -> dict:
    try:
        return question.model_dump(mode="json", exclude={"api_json"})
    except Exception:
        return {
            "id_of_post": getattr(question, "id_of_post", None),
            "page_url": getattr(question, "page_url", None),
            "question_text": getattr(question, "question_text", None),
        }


def record_path(mode: str, question: Any, started: datetime) -> str:
    post = getattr(question, "id_of_post", None) or "unknown"
    return f"questions/{mode}/{started:%Y-%m-%d}/{post}_{started:%H%M%S}.json"


def shadows_path(log_path: str) -> str:
    """The end-of-run shadow results of a question log: '<log>_shadows.json'
    next to it (the log itself is never overwritten)."""
    return log_path.removesuffix(".json") + "_shadows.json"


# Several questions finish at once, and GitHub rejects clashing writes to the
# same branch (409/422) or is briefly unavailable (5xx): try again.
SAVE_ATTEMPTS = 4
SAVE_RETRY_STATUSES = (409, 422, 500, 502, 503, 504)
# End of run: saves that still failed get 3 more tries, 1 minute apart
# (7 Oct: one run's saves all failed, and 45948's log was lost).
RETRY_PASS_TRIES = 3
RETRY_PASS_WAIT_SECONDS = 60.0


class QuestionLogWriter:
    """Writes records to the data repo with DATA_REPO_TOKEN (or nowhere, if unset)."""

    def __init__(self, token: str | None = None, repo: str = DATA_REPO, pause=time.sleep) -> None:  # type: ignore[no-untyped-def]
        self._token = token if token is not None else os.getenv("DATA_REPO_TOKEN")
        self._pause = pause
        self._repo = repo
        self.saved: list[str] = []
        self.failed: dict[str, dict] = {}  # path -> record, for the end-of-run retry pass

    def _put(self, path: str, record: dict) -> None:
        """One save (a few quick attempts on clashes and 5xx); raises on failure."""
        if not self._token:
            raise RuntimeError("DATA_REPO_TOKEN is not set")
        post = record.get("question", {}).get("id_of_post")
        body = {
            "message": f"Question {post} log",
            "content": base64.b64encode(
                json.dumps(to_jsonable(record), indent=2).encode()
            ).decode(),
        }
        for attempt in range(SAVE_ATTEMPTS):
            response = requests.put(
                f"https://api.github.com/repos/{self._repo}/contents/{path}",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                },
                json=body,
                timeout=30,
            )
            if response.status_code not in SAVE_RETRY_STATUSES or attempt == SAVE_ATTEMPTS - 1:
                break
            self._pause(1.0 + attempt)
        response.raise_for_status()

    def save(self, path: str, record: dict) -> bool:
        """Save one record. Never raises; returns False on failure and keeps
        the record for retry_failed."""
        post = record.get("question", {}).get("id_of_post")
        try:
            self._put(path, record)
        except Exception as e:
            logger.warning(
                f"Question {post}: JSON log could not be saved ({type(e).__name__}); retried at the end of the run"
            )
            self.failed[path] = record
            return False
        self.failed.pop(path, None)
        self.saved.append(path)
        return True

    def retry_failed(self) -> int:
        """End of run: try each failed save again, up to RETRY_PASS_TRIES times
        one RETRY_PASS_WAIT_SECONDS apart. Never raises. Each save still failing
        is warned with the annotation the health check looks for. Returns how
        many are still lost."""
        if not self.failed:
            return 0
        for attempt in range(RETRY_PASS_TRIES):
            if attempt:
                self._pause(RETRY_PASS_WAIT_SECONDS)
            for path, record in list(self.failed.items()):
                post = record.get("question", {}).get("id_of_post")
                try:
                    self._put(path, record)
                except Exception as e:
                    logger.warning(f"Question {post}: JSON log retry {attempt + 1} failed ({type(e).__name__})")
                    continue
                del self.failed[path]
                self.saved.append(path)
                logger.info(f"Question {post}: JSON log saved on retry {attempt + 1}")
            if not self.failed:
                return 0
        for record in self.failed.values():
            post = record.get("question", {}).get("id_of_post")
            logger.warning(f"Question {post}: JSON log could not be saved")
            # A GitHub Actions annotation, found by the daily health check.
            print(
                f"::warning title={LOG_FAILURE_ANNOTATION}::"
                f"Question {post}: JSON log could not be saved"
            )
        return len(self.failed)


def questions_per_day(token: str | None, days: int = 7, now: datetime | None = None, get=requests.get) -> float | None:  # type: ignore[no-untyped-def]
    """Distinct live tournament questions we logged per day over the last
    `days` days (from the question-log paths in fall26-data), or None."""
    if not token:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        response = get(
            f"https://api.github.com/repos/{DATA_REPO}/git/trees/HEAD?recursive=1",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=30,
        )
        response.raise_for_status()
        first_day = (now - timedelta(days=days)).date().isoformat()
        posts = set()
        for item in response.json().get("tree", []):
            parts = item.get("path", "").split("/")
            if len(parts) == 4 and parts[:2] == ["questions", "tournament"] and parts[2] > first_day:
                posts.add(parts[3].split("_")[0])
        return len(posts) / days
    except Exception:
        return None

