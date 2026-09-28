"""Per-question retry backoff between runs (main.should_try / update_retry_state)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import main

NOW = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)
FAR = NOW + timedelta(hours=3)


def entry(failures, minutes_ago):
    return {"failures": failures, "last_failure": (NOW - timedelta(minutes=minutes_ago)).isoformat()}


def test_new_question_and_the_next_run_always_try():
    assert main.should_try(None, FAR, NOW)
    assert main.should_try(entry(1, 10), FAR, NOW)  # failed once: the next run tries


def test_then_only_every_30_minutes():
    assert not main.should_try(entry(2, 10), FAR, NOW)
    assert not main.should_try(entry(5, 29), FAR, NOW)
    assert main.should_try(entry(5, 30), FAR, NOW)


def test_every_run_tries_in_the_last_45_minutes():
    assert main.should_try(entry(6, 1), NOW + timedelta(minutes=44), NOW)
    assert not main.should_try(entry(6, 1), NOW + timedelta(minutes=50), NOW)


def test_update_counts_failures_and_clears_successes():
    state = {"1": entry(1, 10), "2": entry(3, 10), "old": entry(2, 60 * 24 * 4)}
    new = main.update_retry_state(state, [1, 2, 3], failed={1, 3}, now=NOW)
    assert new["1"]["failures"] == 2 and new["3"]["failures"] == 1
    assert "2" not in new  # forecast this time
    assert "old" not in new  # over 3 days old
