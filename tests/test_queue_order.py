"""Queue order (main.queue_batches): seasonal first, except MiniBench closing within 30 min."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import main

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def q(post, minutes):
    return SimpleNamespace(id_of_post=post, close_time=NOW + timedelta(minutes=minutes))


def ids(batches):
    return [(seasonal, [x.id_of_post for x in questions]) for seasonal, questions in batches]


def test_seasonal_first_normally():
    assert ids(main.queue_batches([q(1, 600), q(2, 300)], [q(10, 120), q(11, 45)], NOW)) == [
        (True, [1, 2]), (False, [10, 11])
    ]


def test_minibench_closing_within_30_minutes_goes_first():
    batches = main.queue_batches([q(1, 20)], [q(10, 120), q(11, 25), q(12, 10), q(13, 30)], NOW)
    assert ids(batches) == [(False, [12, 11, 13]), (True, [1]), (False, [10])]


def test_empty_batches_dropped_and_naive_times():
    naive = SimpleNamespace(id_of_post=5, close_time=(NOW + timedelta(minutes=5)).replace(tzinfo=None))
    assert ids(main.queue_batches([], [naive], NOW)) == [(False, [5])]
    assert main.queue_batches([], [], NOW) == []
