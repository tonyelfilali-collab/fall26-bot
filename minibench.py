"""
Which MiniBench round is current. MiniBench runs in rounds of a few weeks
(round 33125 closes about 9 Oct 2026), each its own tournament.

1. The 'minibench' slug, if its tournament is running now (Metaculus moves the
   slug to the new round).
2. Otherwise the running tournament named or slugged MiniBench in the API's
   tournament list (a new round with a new id only); the latest start wins.
3. Otherwise the slug's own id (or the slug itself): nothing running yet.

Read-only, one or two API calls per run. A lookup that fails falls back to
the slug, so the live run carries on as before.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Callable

import requests

from bot_helpers import PUBLIC_LOGGER_NAME

logger = logging.getLogger(PUBLIC_LOGGER_NAME)

API = "https://www.metaculus.com/api"
MINIBENCH_SLUG = "minibench"

# fetch(path) -> parsed JSON, or None if the call failed.
Fetch = Callable[[str], "dict | list | None"]


def _metaculus_get(path: str) -> dict | list | None:
    try:
        response = requests.get(
            f"{API}/{path}",
            headers={"Authorization": f"Token {os.environ['METACULUS_TOKEN']}"},
            timeout=30,
        )
    except Exception:
        return None
    return response.json() if response.status_code == 200 else None


def _time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_running(tournament: dict, now: datetime) -> bool:
    start, close = _time(tournament.get("start_date")), _time(tournament.get("close_date"))
    return (start is None or start <= now) and (close is None or now < close)


def _is_minibench(tournament: dict) -> bool:
    text = f"{tournament.get('slug') or ''} {tournament.get('name') or ''}".lower().replace("-", "")
    return "minibench" in text


def current_minibench(fetch: Fetch = _metaculus_get, now: datetime | None = None) -> tuple[int | str, str]:
    """(tournament id, how it was found)."""
    now = now or datetime.now(timezone.utc)
    by_slug = fetch(f"projects/tournaments/{MINIBENCH_SLUG}/")
    if isinstance(by_slug, dict) and by_slug.get("id") and is_running(by_slug, now):
        return by_slug["id"], f"slug '{MINIBENCH_SLUG}'"
    listing = fetch("projects/tournaments/")
    tournaments = listing.get("results", []) if isinstance(listing, dict) else listing or []
    running = [t for t in tournaments if isinstance(t, dict) and t.get("id") and _is_minibench(t) and is_running(t, now)]
    if running:
        newest = max(running, key=lambda t: _time(t.get("start_date")) or datetime.min.replace(tzinfo=timezone.utc))
        return newest["id"], "tournament list (new round, new id)"
    if isinstance(by_slug, dict) and by_slug.get("id"):
        return by_slug["id"], f"slug '{MINIBENCH_SLUG}' (no round running now)"
    return MINIBENCH_SLUG, "slug only (lookup failed)"


def current_minibench_id(fetch: Fetch = _metaculus_get, now: datetime | None = None) -> int | str:
    tournament_id, how = current_minibench(fetch, now)
    logger.info(f"MiniBench: tournament {tournament_id}, found by {how}")
    return tournament_id
