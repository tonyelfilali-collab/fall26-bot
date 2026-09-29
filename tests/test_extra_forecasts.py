"""Build 4: more forecasts when quota is spare (never the reserve). No model calls."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import question_log
from bot_config import GEMINI_FORECAST_MODELS, GeminiPool
from tests.test_gemini_budget import make_pool


def _pool(used=None, expected=4.0, on=True):
    pool = make_pool(used, extra_forecasts=on)
    pool.expected_questions_left_today = lambda now=None: expected
    return pool


def test_spare_quota_gives_5_binary_and_6_numeric_distinct_models_first():
    pool = _pool()  # 4 x 16 = 64 usable; 4 x 4 expected = 16 needed
    binary = pool.plan(seasonal=True, binary=True)
    assert [c.model for c in binary[:4]] == sorted(GEMINI_FORECAST_MODELS, key=GEMINI_FORECAST_MODELS.index)
    assert len(binary) == 5 and pool.last_plan["count"] == 5 and "spare quota" in pool.last_plan["reason"]
    numeric = _pool().plan(seasonal=True, binary=False)
    assert len(numeric) == 6
    counts = Counter(c.model for c in numeric)
    assert len(counts) == 4 and max(counts.values()) == 2  # distinct first, then repeats


def test_extras_never_use_the_reserve():
    pool = _pool()
    chains = pool.plan(seasonal=True, binary=False)
    assert [c._allow_reserve for c in chains] == [True] + [False] * 5  # only the first, as before


def test_not_enough_spare_quota_stays_at_3():
    # 5 usable left on each (20 total); 20 - 3 < 16 + extras? 20 - 4 = 16 >= 16 -> 4 allowed.
    pool = _pool({m: 11 for m in GEMINI_FORECAST_MODELS})
    assert len(pool.plan(seasonal=True, binary=True)) == 4
    pool = _pool({m: 12 for m in GEMINI_FORECAST_MODELS})  # 16 usable: 16 - 4 < 16
    assert len(pool.plan(seasonal=True, binary=True)) == 3
    assert "no spare quota" in pool.last_plan["reason"]


def test_switch_off_keeps_3():
    pool = _pool(on=False)
    assert len(pool.plan(seasonal=True, binary=False)) == 3
    assert pool.last_plan == {"enabled": False, "count": 3, "reason": "extra forecasts switched off"}


def test_minibench_low_budget_still_1():
    pool = _pool({m: 13 for m in GEMINI_FORECAST_MODELS})  # low budget
    assert len(pool.plan(seasonal=False, binary=True)) == 1


@pytest.mark.parametrize(
    "hour_pacific, per_day, expected",
    [(0, None, 4 * 1.0 + 2), (12, 10.0, 10 * 0.5 + 2), (18, 1.0, 4 * 0.25 + 2)],
)
def test_expected_questions_left_today(hour_pacific, per_day, expected):
    from zoneinfo import ZoneInfo

    pool = make_pool(extra_forecasts=True)
    pool.questions_per_day = per_day
    now = datetime(2026, 10, 5, hour_pacific, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert pool.expected_questions_left_today(now) == pytest.approx(expected)


def test_questions_per_day_from_log_paths():
    tree = {"tree": [
        {"path": "questions/tournament/2026-09-27/45516_180200.json"},
        {"path": "questions/tournament/2026-09-28/45847_140200.json"},
        {"path": "questions/tournament/2026-09-28/45847_150200.json"},  # same question again
        {"path": "questions/tournament/2026-09-10/1_000000.json"},  # older than 7 days
        {"path": "questions/test_questions/2026-09-28/43327_000000.json"},  # test run
    ]}
    get = lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None, json=lambda: tree)  # noqa: E731
    now = datetime(2026, 9, 29, 9, tzinfo=timezone.utc)
    assert question_log.questions_per_day("t", now=now, get=get) == pytest.approx(2 / 7)
    assert question_log.questions_per_day(None) is None


def test_minibench_round_active_means_25_a_day():
    from zoneinfo import ZoneInfo

    pool = make_pool(extra_forecasts=True)
    pool.questions_per_day = 1.0
    noon = datetime(2026, 10, 5, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert pool.expected_questions_left_today(noon) == pytest.approx(4 * 0.5 + 2)
    pool.minibench_active = True
    assert pool.expected_questions_left_today(noon) == pytest.approx(25 * 0.5 + 2)


def test_minibench_active_open_now_or_seen_in_24h():
    from datetime import timedelta

    import minibench
    from gemini_budget import MemoryStore

    store = MemoryStore({})
    now = datetime(2026, 10, 5, 10, tzinfo=timezone.utc)
    assert minibench.minibench_active(0, now, store) is False
    assert minibench.minibench_active(2, now, store) is True  # open now: remembered
    assert minibench.minibench_active(0, now + timedelta(hours=23), store) is True
    assert minibench.minibench_active(0, now + timedelta(hours=25), store) is False


def test_minibench_active_limits_extra_forecasts():
    pool = _pool(expected=25 * 0.5 + 2)  # an active round at noon: 14.5 expected -> 58 needed
    assert len(pool.plan(seasonal=True, binary=True)) == 5  # 64 - 5 = 59 >= 58
    pool = _pool({m: 1 for m in GEMINI_FORECAST_MODELS}, expected=25 * 0.5 + 2)  # 60 usable
    assert len(pool.plan(seasonal=True, binary=True)) == 3
