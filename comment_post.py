"""
The private comment that goes with each forecast (architect, 22 Oct: comments
are required for prizes). The library posts it right after the forecast;
this client posts it the same way but keeps the answer: the HTTP status and
the comment id go to the private question log ("comment"). A failed post never
raises (the forecast is already in) and leaves a "comment-failed" warning
annotation, which the daily health check watches. The public log gets the
status only, never the comment text.

Test Bot runs also read the comment back (by id, then by post + author) and
log yes/no with the HTTP statuses.
"""
from __future__ import annotations

import logging
from typing import Any

import requests
from forecasting_tools import MetaculusClient

from bot_helpers import PUBLIC_LOGGER_NAME

logger = logging.getLogger(PUBLIC_LOGGER_NAME)

COMMENT_FAILED_ANNOTATION = "comment-failed"
BOT_USER_ID = 309271  # Quillfeather-Vex


def _ok(status: Any) -> bool:
    return isinstance(status, int) and 200 <= status < 300


class CommentRecordingClient(MetaculusClient):
    """MetaculusClient whose post_question_comment records its answer in
    `self.outcome` (status, id) and never raises."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.outcome: dict = {}

    def post_question_comment(  # type: ignore[override]
        self, post_id: int, comment_text: str, is_private: bool = True, included_forecast: bool = True
    ) -> None:
        self._sleep_between_requests()
        status: Any
        comment_id = None
        try:
            response = requests.post(
                f"{self.base_url}/comments/create/",
                json={"on_post": post_id, "text": comment_text, "is_private": is_private,
                      "included_forecast": included_forecast},
                **self._get_auth_headers(),  # type: ignore[arg-type]
                timeout=self.timeout,
            )
            status = response.status_code
            try:
                body = response.json()
                comment_id = body.get("id") if isinstance(body, dict) else None
            except ValueError:
                pass
        except Exception as e:
            status = f"error ({type(e).__name__})"
        self.outcome = {"status": status, "id": comment_id, "private": is_private}
        if _ok(status):
            logger.info(f"Question {post_id}: comment posted (HTTP {status}, id {'returned' if comment_id else 'not returned'})")
        else:
            logger.warning(f"Question {post_id}: comment NOT posted (HTTP {status})")
            print(f"::warning title={COMMENT_FAILED_ANNOTATION}::Question {post_id}: private comment not posted ({status})")

    def read_back(self, post_id: int, comment_id: Any = None, author: int = BOT_USER_ID, get=None) -> dict:  # type: ignore[no-untyped-def]
        """Is our comment there? By id (if the create answer gave one), then by
        post + author. Statuses and yes/no only."""
        get = get or requests.get
        found: dict = {}
        headers = self._get_auth_headers()["headers"]
        if comment_id:
            response = get(f"{self.base_url}/comments/{comment_id}/", headers=headers, timeout=self.timeout)
            found["by_id_status"] = response.status_code
            found["by_id"] = _ok(response.status_code)
        response = get(f"{self.base_url}/comments/", params={"post": post_id, "author": author, "limit": 100},
                       headers=headers, timeout=self.timeout)
        found["by_post_status"] = response.status_code
        results: list = []
        if response.status_code == 200:
            data = response.json()
            results = data.get("results", []) if isinstance(data, dict) else data
        mine = [c for c in results if isinstance(c, dict) and _author(c) == author]
        found["by_post_count"] = len(mine)
        found["by_post"] = any(c.get("id") == comment_id for c in mine) if comment_id else bool(mine)
        found["exists"] = bool(found.get("by_id") or found["by_post"])
        return found


def _author(comment: dict) -> Any:
    author = comment.get("author")
    return author.get("id") if isinstance(author, dict) else author
