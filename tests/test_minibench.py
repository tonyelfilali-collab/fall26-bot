"""MiniBench rollover (minibench.py) with fake Metaculus API replies."""
from __future__ import annotations

from datetime import datetime, timezone

import health_check
from gemini_budget import MemoryStore
from minibench import SCAN_WINDOW, current_minibench, current_minibench_id

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
OLD_ROUND = {"id": 33125, "slug": "minibench", "name": "MiniBench",
             "start_date": "2026-09-21T00:00:00Z", "close_date": "2026-10-09T00:00:00Z"}
NEW_ROUND = {"id": 33160, "slug": "minibench-2026-10-09", "name": "MiniBench",
             "start_date": "2026-10-09T00:00:00Z", "close_date": "2026-10-30T00:00:00Z"}
OTHER = {"id": 33130, "slug": "some-other-series", "name": "Something else",
         "start_date": "2026-09-28T00:00:00Z", "close_date": "2027-03-05T00:00:00Z"}


def fake(tournaments: dict):
    """tournaments: path key (slug or id) -> tournament JSON; anything else is a 404."""
    calls = []

    def fetch(path):
        calls.append(path)
        key = path.removeprefix("projects/tournaments/").rstrip("/")
        return tournaments.get(key)

    fetch.calls = calls
    return fetch


def no_pause(_seconds):
    return None


def test_current_round_by_slug_plus_a_small_scan():
    fetch = fake({"minibench": OLD_ROUND})
    store = MemoryStore()
    assert current_minibench(fetch, datetime(2026, 9, 28, tzinfo=timezone.utc), store, no_pause)[0] == 33125
    assert fetch.calls[0] == "projects/tournaments/minibench/"
    assert len(fetch.calls) == 1 + SCAN_WINDOW  # every run looks at the next ids
    assert store.load()["id"] == 33125


def test_slug_moved_to_the_new_round():
    fetch = fake({"minibench": {**NEW_ROUND, "slug": "minibench"}})
    assert current_minibench_id(fetch, NOW, MemoryStore()) == 33160


def test_new_round_while_the_old_one_is_still_running_by_its_dates():
    # 5 Oct: the old round runs to 9 Oct and keeps the slug; a new round with a
    # new id has started. The newest running round wins (the old one's
    # questions are over).
    oct5 = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
    early = {**NEW_ROUND, "id": 33135, "start_date": "2026-10-05T00:00:00Z"}
    store = MemoryStore({"id": 33125, "max_seen": 33125})
    fetch = fake({"minibench": OLD_ROUND, "33125": OLD_ROUND, "33130": OTHER, "33135": early})
    tournament_id, how = current_minibench(fetch, oct5, store, no_pause)
    assert tournament_id == 33135 and "scan" in how
    assert store.load()["id"] == 33135 and store.load()["max_seen"] == 33135


def test_upcoming_round_found_before_it_starts_then_used_at_start():
    oct4 = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
    upcoming = {**NEW_ROUND, "id": 33135, "start_date": "2026-10-05T00:00:00Z"}
    store = MemoryStore({"id": 33125, "max_seen": 33125})
    tournaments = {"minibench": OLD_ROUND, "33125": OLD_ROUND, "33135": upcoming}
    assert current_minibench(fake(tournaments), oct4, store, no_pause)[0] == 33125  # not started yet
    assert store.load()["next_id"] == 33135
    # The first run after it starts: the remembered next round, found without a new scan hit.
    oct5 = datetime(2026, 10, 5, 0, 10, tzinfo=timezone.utc)
    tournament_id, how = current_minibench(fake(tournaments), oct5, store, no_pause)
    assert tournament_id == 33135 and how == "remembered next round"


def test_scan_moves_up_as_new_ids_appear():
    store = MemoryStore({"id": 33125, "max_seen": 33125})
    first = fake({"minibench": OLD_ROUND, "33130": OTHER})
    current_minibench(first, NOW, store, no_pause)
    assert store.load()["max_seen"] == 33130
    second = fake({"minibench": OLD_ROUND})
    current_minibench(second, NOW, store, no_pause)
    assert second.calls[-SCAN_WINDOW] == "projects/tournaments/33131/"  # starts above the highest seen


def test_lookup_failure_falls_back_to_the_slug():
    assert current_minibench(fake({}), NOW, MemoryStore(), no_pause)[0] == "minibench"


def test_health_warns_not_red_when_minibench_is_quiet():
    report = health_check.Report()
    health_check.check_minibench_activity(0, report)
    assert report.warnings and not report.red
    ok = health_check.Report()
    health_check.check_minibench_activity(4, ok)
    assert ok.ok and not ok.warnings and not ok.red
