"""Build 2b: the random-walk baseline shadow (zero model calls)."""
from __future__ import annotations

import asyncio
import random
from datetime import date, datetime, timedelta, timezone

import pytest
from forecasting_tools import NumericQuestion

import main
import scoreboard
from forecast_safety import distribution_problems
from question_log import to_jsonable
from stat_baseline import random_walk_baseline, random_walk_percentiles

TODAY = date(2026, 9, 28)


def history(start=4.0, days=500, step=0.05, seed=1):
    rng = random.Random(seed)
    value, rows = start, []
    for i in range(days):
        rows.append([(TODAY - timedelta(days=days - i)).isoformat(), round(value, 4)])
        value += rng.gauss(0, step)
    return rows


def question(resolve_days=30, lower=2.0, upper=7.0):
    return NumericQuestion(
        question_text="What will the 10-year Treasury yield be?", id_of_post=9, unit_of_measure="%",
        lower_bound=lower, upper_bound=upper, open_lower_bound=True, open_upper_bound=True,
        zero_point=None, cdf_size=201,
        scheduled_resolution_time=datetime.combine(TODAY + timedelta(days=resolve_days), datetime.min.time(), timezone.utc),
    )


DATA = {"source": "fred", "series": "DGS10", "history": history(), "latest": {}}


def test_baseline_is_centred_on_the_latest_value_and_platform_valid():
    q = question()
    distribution, detail = random_walk_baseline(q, DATA)
    assert distribution_problems(distribution, q) == []
    latest = DATA["history"][-1][1]
    median = [p.value for p in distribution.get_cdf() if p.percentile >= 0.5][0]
    assert median == pytest.approx(latest, abs=0.05)
    assert detail["log_changes"] is False and detail["horizon_days"] == 31


def test_spread_grows_with_time_to_resolve():
    near, _ = random_walk_percentiles(DATA, TODAY + timedelta(days=5))
    far, _ = random_walk_percentiles(DATA, TODAY + timedelta(days=120))
    width = lambda ps: ps[-1].value - ps[0].value  # noqa: E731
    assert width(far) == pytest.approx(width(near) * (121 / 6) ** 0.5, rel=1e-6)


def test_prices_use_log_changes_and_stay_positive():
    prices = {"source": "coingecko", "series": "bitcoin", "history": history(start=60000, step=900, days=365)}
    percentiles, detail = random_walk_percentiles(prices, TODAY + timedelta(days=60))
    assert detail["log_changes"] is True and all(p.value > 0 for p in percentiles)


def test_no_baseline_without_enough_data_or_a_resolve_date():
    assert random_walk_baseline(question(), {"history": history(days=5)}) is None
    q = question()
    q.scheduled_resolution_time = None
    q.close_time = None
    assert random_walk_baseline(q, DATA) is None


def test_saved_as_a_shadow_and_scored():
    q = question()
    bot = main.FallBot2026(
        llms={"default": "openrouter/x:free", "parser": "openrouter/x:free", "summarizer": "openrouter/x:free", "researcher": "no_research"},
        publish_reports_to_metaculus=False,
    )
    record = bot._record_for(q)

    async def found():
        return dict(DATA, latest={"date": DATA["history"][-1][0], "value": DATA["history"][-1][1]})

    asyncio.run(bot._attach_hard_data(q, record, found()))
    assert "random-walk" in record["shadow"] and record["hard_data"]["baseline"]["horizon_days"] == 31
    live = to_jsonable(record["shadow"]["random-walk"])
    rows = scoreboard.score_records(
        [{"question": {"id_of_post": 9, "question_type": "numeric"}, "final_forecast": live, "shadow": record["shadow"]}],
        {9: str(DATA["history"][-1][1])}.get,
    )
    assert "| numeric | random-walk | 1 |" in scoreboard.report_markdown(rows)
