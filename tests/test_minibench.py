"""MiniBench rollover (minibench.py) with fake Metaculus API replies."""
from __future__ import annotations

from datetime import datetime, timezone

import health_check
from gemini_budget import MemoryStore
from minibench import SCAN_LIMIT, current_minibench, current_minibench_id

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


def test_current_round_by_slug_is_one_call():
    fetch = fake({"minibench": OLD_ROUND})
    store = MemoryStore()
    assert current_minibench(fetch, datetime(2026, 9, 28, tzinfo=timezone.utc), store, no_pause)[0] == 33125
    assert fetch.calls == ["projects/tournaments/minibench/"]
    assert store.load()["id"] == 33125


def test_slug_moved_to_the_new_round():
    fetch = fake({"minibench": {**NEW_ROUND, "slug": "minibench"}})
    assert current_minibench_id(fetch, NOW, MemoryStore()) == 33160


def test_new_id_only_found_by_scanning_and_remembered():
    store = MemoryStore()
    fetch = fake({"minibench": OLD_ROUND, "33125": OLD_ROUND, "33130": OTHER, "33160": NEW_ROUND})
    tournament_id, how = current_minibench(fetch, NOW, store, no_pause)
    assert tournament_id == 33160 and "scan" in how
    assert store.load()["id"] == 33160
    # The next run: slug + the remembered round, no scan.
    later = fake({"minibench": OLD_ROUND, "33160": NEW_ROUND})
    tournament_id, how = current_minibench(later, NOW, store, no_pause)
    assert tournament_id == 33160 and how == "remembered round"
    assert len(later.calls) == 2


def test_scan_is_limited_and_at_most_hourly():
    store = MemoryStore()
    fetch = fake({"minibench": OLD_ROUND})  # no new round anywhere
    assert current_minibench(fetch, NOW, store, no_pause)[0] == 33125  # the slug's id
    assert len(fetch.calls) == 1 + SCAN_LIMIT
    again = fake({"minibench": OLD_ROUND})
    current_minibench(again, NOW, store, no_pause)
    assert len(again.calls) == 1  # scanned less than an hour ago


def test_lookup_failure_falls_back_to_the_slug():
    assert current_minibench(fake({}), NOW, MemoryStore(), no_pause)[0] == "minibench"


def test_health_warns_not_red_when_minibench_is_quiet():
    report = health_check.Report()
    health_check.check_minibench_activity(0, report)
    assert report.warnings and not report.red
    ok = health_check.Report()
    health_check.check_minibench_activity(4, ok)
    assert ok.ok and not ok.warnings and not ok.red
