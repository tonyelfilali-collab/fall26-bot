"""
Which MiniBench round is current. MiniBench runs in rounds of a few weeks
(round 33125 closes about 9 Oct 2026), each its own tournament. MiniBench
rounds are unlisted, so the API's tournament list doesn't show them.

1. The 'minibench' slug, if its tournament is running now (Metaculus moves the
   slug to the new round).
2. Otherwise the round remembered in fall26-data (status/minibench.json), if
   it is running.
3. Otherwise a new round with a new id only: look up the next tournament ids
   above the last known round, one a second, at most SCAN_LIMIT; the first
   running MiniBench is used and remembered, so later runs need one call.
4. Otherwise the slug's own id (or the slug itself): nothing running yet.

Read-only on Metaculus. A lookup that fails falls back to the slug, so the
live run carries on as before.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

import requests

from bot_helpers import PUBLIC_LOGGER_NAME
from gemini_budget import GitHubFileStore, MemoryStore

logger = logging.getLogger(PUBLIC_LOGGER_NAME)

API = "https://www.metaculus.com/api"
MINIBENCH_SLUG = "minibench"
STATUS_PATH = "status/minibench.json"
# Tournament ids are shared with other projects (33124 and 33126 don't exist,
# for example), so a new round can be a few dozen ids further on.
SCAN_LIMIT = 60
SCAN_PAUSE_SECONDS = 1.0
# Between rounds, scan at most once an hour (runs are every 10 minutes).
SCAN_EVERY = timedelta(hours=1)

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


def _default_store() -> MemoryStore | GitHubFileStore:
    token = os.getenv("DATA_REPO_TOKEN")
    return GitHubFileStore("tonyelfilali-collab/fall26-data", STATUS_PATH, token) if token else MemoryStore()


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


def is_minibench(tournament: dict) -> bool:
    text = f"{tournament.get('slug') or ''} {tournament.get('name') or ''}".lower().replace("-", "")
    return "minibench" in text


def _tournament(fetch: Fetch, key: int | str) -> dict | None:
    found = fetch(f"projects/tournaments/{key}/")
    return found if isinstance(found, dict) and found.get("id") else None


def _update(store, saved: dict, **changes) -> None:
    try:
        store.save({**saved, **changes})
    except Exception:
        logger.warning("MiniBench: status could not be saved")


def _remember(store, saved: dict, tournament: dict) -> None:
    if saved.get("id") != tournament["id"]:
        _update(store, saved, id=tournament["id"], slug=tournament.get("slug"),
                close_date=tournament.get("close_date"))


def current_minibench(
    fetch: Fetch = _metaculus_get,
    now: datetime | None = None,
    store=None,
    pause: Callable[[float], None] = time.sleep,
) -> tuple[int | str, str]:
    """(tournament id, how it was found)."""
    now = now or datetime.now(timezone.utc)
    store = store if store is not None else _default_store()
    try:
        saved = store.load() or {}
    except Exception:
        saved = {}
    by_slug = _tournament(fetch, MINIBENCH_SLUG)
    if by_slug and is_running(by_slug, now):
        _remember(store, saved, by_slug)
        return by_slug["id"], f"slug '{MINIBENCH_SLUG}'"
    remembered_id = saved.get("id")
    if remembered_id and remembered_id != (by_slug or {}).get("id"):
        remembered = _tournament(fetch, remembered_id)
        if remembered and is_minibench(remembered) and is_running(remembered, now):
            return remembered["id"], "remembered round"
    known = [i for i in ((by_slug or {}).get("id"), remembered_id) if isinstance(i, int)]
    last_scan = _time(saved.get("last_scan"))
    if known and (last_scan is None or now - last_scan >= SCAN_EVERY):
        _update(store, saved, last_scan=now.isoformat())
        for candidate in range(max(known) + 1, max(known) + 1 + SCAN_LIMIT):
            pause(SCAN_PAUSE_SECONDS)
            found = _tournament(fetch, candidate)
            if found and is_minibench(found) and is_running(found, now):
                _remember(store, {**saved, "last_scan": now.isoformat()}, found)
                return found["id"], f"id scan (new round, new id; scanned up to {candidate})"
    if by_slug:
        return by_slug["id"], f"slug '{MINIBENCH_SLUG}' (no round running now)"
    return MINIBENCH_SLUG, "slug only (lookup failed)"


def current_minibench_id(fetch: Fetch = _metaculus_get, now: datetime | None = None, store=None) -> int | str:
    tournament_id, how = current_minibench(fetch, now, store)
    logger.info(f"MiniBench: tournament {tournament_id}, found by {how}")
    return tournament_id


# Build 4b (architect, 29 Sep): while a MiniBench round is active (a question
# open now, or one seen open in the last 24 hours), the spare-quota rule
# expects 25 questions a day (the last round: 60 in about 3 days).
ACTIVE_FOR = timedelta(hours=24)


def minibench_active(open_now: int, now: datetime | None = None, store=None) -> bool:
    """Notes when a MiniBench question was last seen open (status/minibench.json)
    and says whether the round counts as active. Never raises."""
    now = now or datetime.now(timezone.utc)
    store = store if store is not None else _default_store()
    try:
        saved = store.load() or {}
    except Exception:
        saved = {}
    if open_now > 0:
        _update(store, saved, last_open_seen=now.isoformat())
        return True
    last = _time(saved.get("last_open_seen"))
    return last is not None and now - last <= ACTIVE_FOR

