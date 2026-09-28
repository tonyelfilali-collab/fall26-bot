"""MiniBench rollover (minibench.py) with fake Metaculus API replies."""
from __future__ import annotations

from datetime import datetime, timezone

import health_check
from minibench import current_minibench, current_minibench_id

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
OLD_ROUND = {"id": 33125, "slug": "minibench", "name": "MiniBench",
             "start_date": "2026-09-21T00:00:00Z", "close_date": "2026-10-09T00:00:00Z"}
NEW_ROUND = {"id": 33190, "slug": "minibench-2026-10-09", "name": "MiniBench",
             "start_date": "2026-10-09T00:00:00Z", "close_date": "2026-10-30T00:00:00Z"}
OTHER = {"id": 33121, "slug": "fall-futureeval-2026", "name": "Fall 2026 FutureEval Bot Tournament",
         "start_date": "2026-09-28T00:00:00Z", "close_date": "2027-03-05T00:00:00Z"}


def fake(replies: dict):
    calls = []

    def fetch(path):
        calls.append(path)
        return replies.get(path)

    fetch.calls = calls
    return fetch


def test_current_round_by_slug():
    fetch = fake({"projects/tournaments/minibench/": OLD_ROUND})
    assert current_minibench(fetch, datetime(2026, 9, 28, tzinfo=timezone.utc))[0] == 33125
    assert fetch.calls == ["projects/tournaments/minibench/"]  # one call when the slug is current


def test_slug_moved_to_the_new_round():
    fetch = fake({"projects/tournaments/minibench/": {**NEW_ROUND, "slug": "minibench"}})
    assert current_minibench_id(fetch, NOW) == 33190


def test_new_id_only_found_in_the_tournament_list():
    fetch = fake({
        "projects/tournaments/minibench/": OLD_ROUND,
        "projects/tournaments/": [OTHER, OLD_ROUND, NEW_ROUND],
    })
    tournament_id, how = current_minibench(fetch, NOW)
    assert tournament_id == 33190 and "list" in how


def test_list_as_paged_results_and_latest_start_wins():
    newer = {**NEW_ROUND, "id": 33200, "start_date": "2026-10-11T00:00:00Z"}
    fetch = fake({
        "projects/tournaments/minibench/": OLD_ROUND,
        "projects/tournaments/": {"results": [NEW_ROUND, newer, OTHER]},
    })
    assert current_minibench_id(fetch, NOW) == 33200


def test_no_round_running_keeps_the_slug_id():
    fetch = fake({"projects/tournaments/minibench/": OLD_ROUND, "projects/tournaments/": [OTHER, OLD_ROUND]})
    assert current_minibench_id(fetch, NOW) == 33125


def test_lookup_failure_falls_back_to_the_slug():
    assert current_minibench_id(fake({}), NOW) == "minibench"


def test_health_warns_not_red_when_minibench_is_quiet():
    report = health_check.Report()
    health_check.check_minibench_activity(0, report)
    assert report.warnings and not report.red
    ok = health_check.Report()
    health_check.check_minibench_activity(4, ok)
    assert ok.ok and not ok.warnings and not ok.red
