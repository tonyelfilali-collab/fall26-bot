"""
One JSON file per forecast question, saved to the private fall26-data repo:
the question snapshot, the research (with its time), each forecast's models,
raw output, parsed value and timing, and the final forecast.

Saving happens after the forecast is submitted and never raises: a failure is
logged and marked with a GitHub Actions warning annotation (title
"question-log-failed"), which the daily health check looks for.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone
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


class QuestionLogWriter:
    """Writes records to the data repo with DATA_REPO_TOKEN (or nowhere, if unset)."""

    def __init__(self, token: str | None = None, repo: str = DATA_REPO) -> None:
        self._token = token if token is not None else os.getenv("DATA_REPO_TOKEN")
        self._repo = repo
        self.saved: list[str] = []

    def save(self, path: str, record: dict) -> bool:
        """Save one record. Never raises; returns False (and warns) on failure."""
        post = record.get("question", {}).get("id_of_post")
        try:
            if not self._token:
                raise RuntimeError("DATA_REPO_TOKEN is not set")
            response = requests.put(
                f"https://api.github.com/repos/{self._repo}/contents/{path}",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                },
                json={
                    "message": f"Question {post} log",
                    "content": base64.b64encode(
                        json.dumps(to_jsonable(record), indent=2).encode()
                    ).decode(),
                },
                timeout=30,
            )
            response.raise_for_status()
        except Exception as e:
            logger.warning(
                f"Question {post}: JSON log could not be saved ({type(e).__name__})"
            )
            # A GitHub Actions annotation, found by the daily health check.
            print(
                f"::warning title={LOG_FAILURE_ANNOTATION}::"
                f"Question {post}: JSON log could not be saved"
            )
            return False
        self.saved.append(path)
        return True
