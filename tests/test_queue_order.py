"""Run queue (main.run_queue): seasonal and MiniBench in one queue, soonest-closing first."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import main

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def q(post, minutes):
    return SimpleNamespace(id_of_post=post, close_time=NOW + timedelta(minutes=minutes))


def test_one_queue_soonest_closing_first():
    queued, seasonal = main.run_queue([q(1, 600), q(2, 20)], [q(10, 120), q(11, 45), q(12, 10)])
    assert [x.id_of_post for x in queued] == [12, 2, 11, 10, 1]
    assert seasonal == {1: True, 2: True, 10: False, 11: False, 12: False}


def test_naive_times_and_no_close_time_last():
    naive = SimpleNamespace(id_of_post=5, close_time=(NOW + timedelta(minutes=5)).replace(tzinfo=None))
    none = SimpleNamespace(id_of_post=6, close_time=None)
    queued, _ = main.run_queue([none], [q(7, 60), naive])
    assert [x.id_of_post for x in queued] == [5, 7, 6]
    assert main.run_queue([], []) == ([], {})
