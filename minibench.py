"""
Which MiniBench round is current. MiniBench runs in rounds of a few weeks
(round 33125 closes about 9 Oct 2026), each its own tournament. MiniBench
rounds are unlisted, so the API's tournament list doesn't show them.

Every run (29 Sep: a new round must be found within one run, ~10 minutes):
1. The 'minibench' slug, if its tournament is running now.
2. The rounds remembered in fall26-data (status/minibench.json): the current
   one and a "next" one found before it started.
3. A small scan of the next SCAN_WINDOW tournament ids above the highest id
   seen so far (tournament ids only grow). A MiniBench round found there,
   running or not yet started, is remembered as the next round.
The NEWEST running MiniBench round wins: a new round can start while the old
one is still "running" by its dates (its questions are over by then), and the
slug may stay on the old one. Nothing running: the slug's own id (or the slug).

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
# Each run looks at this many ids above the highest tournament id seen so far
# (ids are shared with other projects, and some don't exist, e.g. 33124).
SCAN_WINDOW = 20
SCAN_PAUSE_SECONDS = 0.25

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


def _closed(tournament: dict, now: datetime) -> bool:
    close = _time(tournament.get("close_date"))
    return close is not None and close <= now


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
    running: dict[int, str] = {}
    by_slug = _tournament(fetch, MINIBENCH_SLUG)
    if by_slug and is_running(by_slug, now):
        running[by_slug["id"]] = f"slug '{MINIBENCH_SLUG}'"
    for key, how in ((saved.get("id"), "remembered round"), (saved.get("next_id"), "remembered next round")):
        if isinstance(key, int) and key not in running and key != (by_slug or {}).get("id"):
            found = _tournament(fetch, key)
            if found and is_minibench(found) and is_running(found, now):
                running[key] = how
    known = [i for i in ((by_slug or {}).get("id"), saved.get("id"), saved.get("next_id"), saved.get("max_seen"))
             if isinstance(i, int)]
    changes: dict = {}
    if known:
        max_seen = max(known)
        next_id = saved.get("next_id")
        for candidate in range(max_seen + 1, max_seen + 1 + SCAN_WINDOW):
            pause(SCAN_PAUSE_SECONDS)
            found = _tournament(fetch, candidate)
            if found is None:
                continue
            changes["max_seen"] = candidate
            if is_minibench(found) and not _closed(found, now):
                next_id = candidate
                changes.update(next_id=candidate, next_start=found.get("start_date"))
                if is_running(found, now):
                    running[candidate] = f"id scan (new round, new id {candidate})"
        if next_id and next_id != saved.get("next_id"):
            logger.info(f"MiniBench: next round found: tournament {next_id} (starts {changes.get('next_start')})")
    if running:
        newest = max(running)
        if saved.get("id") != newest:
            changes.update(id=newest)
        if changes:
            _update(store, saved, **changes)
        return newest, running[newest]
    if changes:
        _update(store, saved, **changes)
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

