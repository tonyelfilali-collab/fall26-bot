"""Empirical window baseline (architect, 1 Oct): shadow only, zero model calls."""
from __future__ import annotations

import asyncio
import random
from datetime import date, datetime, timedelta, timezone

import pytest
from forecasting_tools import NumericQuestion

import main
import scoreboard
import window_baseline as wb
from forecast_safety import distribution_problems
from question_log import to_jsonable

TODAY = date(2026, 9, 30)


def history(values, start=date(2000, 1, 1)):
    return [[(start + timedelta(days=i)).isoformat(), v] for i, v in enumerate(values)]


def question(text="What will the value be on December 25?", days=30, lower=6.0, upper=40.0, open_upper=True, open_lower=True):
    opened = datetime(2026, 9, 30, 14, tzinfo=timezone.utc)
    return NumericQuestion(
        question_text=text, id_of_post=7, unit_of_measure="Units", lower_bound=lower, upper_bound=upper,
        open_lower_bound=open_lower, open_upper_bound=open_upper, zero_point=None, cdf_size=201,
        open_time=opened, scheduled_resolution_time=opened + timedelta(days=days),
    )


def found(series="VIXCLS", value=16.0, source="fred"):
    return {"source": source, "series": series, "latest": {"date": TODAY.isoformat(), "value": value}}


def test_statistic_from_the_question_wording():
    assert wb.statistic_for(question("What will be the highest intraday value of the VIX?")) == "max"
    assert wb.statistic_for(question("What will be the lowest close of the S&P 500?")) == "min"
    assert wb.statistic_for(question("What will the 10-year yield be on Dec 31?")) == "end"
    assert wb.statistic_for(question("What will the high-yield spread be on Dec 31?")) == "end"  # not "high"
    assert wb.statistic_for(question("What will the average VIX be in November?")) is None


def test_end_max_min_relative_to_the_window_start():
    # A saw: up 1 a day for 10 days, back down; ratio scaling on today's 10.
    values = [10 + (i % 20 if i % 20 < 10 else 20 - i % 20) for i in range(400)]
    h = history(values)
    for stat in ("end", "max", "min"):
        sample, detail = wb.window_sample(h, 10, stat, today_value=10.0, ratio=True)
        # Today at 10: the windows starting at 10-12 (within 25%) are used.
        assert detail["statistic"] == stat and detail["windows"] == len(sample) >= wb.MIN_SIMILAR_WINDOWS
        assert detail["similar_level"]
    sample, _ = wb.window_sample(h, 10, "max", today_value=10.0, ratio=True)
    assert max(sample) == pytest.approx(20.0)  # from 10 up to 20: ratio 2 x today 10
    sample, _ = wb.window_sample(h, 10, "min", today_value=20.0, ratio=True)
    assert min(sample) == pytest.approx(10.0)  # from 20 down to 10: ratio 0.5 x today 20
    assert all(m >= e for m, e in zip(wb.window_sample(h, 10, "max", 10.0, True)[0], wb.window_sample(h, 10, "end", 10.0, True)[0]))


def test_change_scaling_for_rates():
    h = history([1.0 + 0.01 * i for i in range(200)])  # +0.01 a day
    sample, detail = wb.window_sample(h, 30, "end", today_value=4.0, ratio=False)
    assert detail["scaling"] == "change" and sample[0] == pytest.approx(4.3)


def test_similar_level_windows_preferred_else_all():
    low, high = [10.0] * 300, [30.0] * 300
    h = history(low + high)
    _, detail = wb.window_sample(h, 20, "end", today_value=11.0, ratio=True)
    assert detail["similar_level"] and detail["windows"] == detail["similar_windows"] < detail["all_windows"]
    few = history([10.0] * 20 + [30.0] * 500)  # only 20 windows start near 11
    _, detail = wb.window_sample(few, 20, "end", today_value=11.0, ratio=True)
    assert not detail["similar_level"] and detail["windows"] == detail["all_windows"]


def test_too_few_windows_or_an_average_means_no_baseline():
    q = question(days=30)
    distribution, detail = wb.window_baseline(q, found(), history([16.0 + (i % 7) for i in range(50)]))
    assert distribution is None and detail["skipped"].startswith("too few windows")
    distribution, detail = wb.window_baseline(question("What will the average VIX be?"), found(), history([16.0] * 2000))
    assert distribution is None and "average" in detail["skipped"]


def test_platform_valid_within_the_question_bounds_and_intraday_warning():
    rng = random.Random(3)
    values, v = [], 16.0
    for _ in range(6000):
        v = max(9.0, v * (1 + rng.gauss(0, 0.06)))
        values.append(round(v, 2))
    h = history(values)
    for open_upper, open_lower in ((True, True), (False, False)):
        q = question("What will be the highest intraday value of the VIX between October 1 and December 24?", days=86,
                     open_upper=open_upper, open_lower=open_lower)
        distribution, detail = wb.window_baseline(q, found(value=16.0), h)
        assert distribution is not None and distribution_problems(distribution, q) == []
        assert detail["statistic"] == "max" and detail["windows"] >= wb.MIN_SIMILAR_WINDOWS
        assert "intraday maximum" in detail["warning"]
        # A maximum over the window is never below today's value (ratio >= 1).
        assert detail["percentiles"]["0.025"] >= 16.0 - 1e-6


def test_saved_as_a_shadow_and_scored(monkeypatch):
    q = question("What will be the highest intraday value of the VIX?", days=60)
    values = [16.0 + 4 * ((i % 50) / 50) for i in range(3000)]
    monkeypatch.setattr(main, "fetch_full_history", lambda f: history(values))
    bot = main.FallBot2026(
        llms={"default": "openrouter/x:free", "parser": "openrouter/x:free", "summarizer": "openrouter/x:free", "researcher": "no_research"},
        publish_reports_to_metaculus=False,
    )
    record = bot._record_for(q)

    async def data():
        return dict(found(), history=history(values[-500:]))

    asyncio.run(bot._attach_hard_data(q, record, data()))
    assert "window" in record["shadow"] and "random-walk" not in record["shadow"]
    assert record["hard_data"]["window_baseline"]["statistic"] == "max"
    shadow = to_jsonable(record["shadow"]["window"])
    rows = scoreboard.score_records(
        [{"question": {"id_of_post": 7, "question_type": "numeric"}, "final_forecast": shadow, "shadow": record["shadow"]}],
        {7: "18.0"}.get,
    )
    assert "| numeric | window | 1 |" in scoreboard.report_markdown(rows)


def test_a_failed_history_fetch_is_only_logged(monkeypatch):
    def broken(f):
        raise ConnectionError("down")

    monkeypatch.setattr(main, "fetch_full_history", broken)
    bot = main.FallBot2026(
        llms={"default": "openrouter/x:free", "parser": "openrouter/x:free", "summarizer": "openrouter/x:free", "researcher": "no_research"},
        publish_reports_to_metaculus=False,
    )
    q = question()
    record = bot._record_for(q)

    async def data():
        return found()

    asyncio.run(bot._attach_hard_data(q, record, data()))
    assert "window" not in (record.get("shadow") or {})
    assert record["hard_data"]["window_baseline"]["skipped"].startswith("failed")


def test_a_gap_in_the_history_does_not_stop_the_windows():
    # 1 Oct: a 7-day market closure (like Sep 2001) once stopped every later window.
    values = history([10.0] * 400)
    gapped = values[:150] + values[157:]
    _, detail = wb.window_sample(gapped, 20, "end", today_value=10.0, ratio=True)
    # Starts on days 0-379 fit a 20-day window before day 399; 7 of them are in the gap: 373.
    assert detail["all_windows"] == 380 - 7
