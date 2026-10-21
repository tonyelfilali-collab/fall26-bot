"""Zero-cost shadow "window-blend" (21 Oct): 50/50 pointwise blend of the live
CDF and the window baseline's. Never submitted; 0 model calls."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest
from forecasting_tools import DiscreteQuestion, NumericQuestion, Percentile

import bot_config
import main
import replay
import scoreboard
import shadow
from distributions import _declared_cdf, pchip_distribution, mix_with_uniform
from forecast_safety import distribution_problems
from question_log import to_jsonable

OPENED = datetime(2026, 9, 30, 14, tzinfo=timezone.utc)


def numeric(open_lower=True, open_upper=True, **kw):
    fields = dict(question_text="What will be the highest value of the VIX?", id_of_post=7, page_url="https://www.metaculus.com/questions/7",
                  unit_of_measure="Units", lower_bound=6.0, upper_bound=40.0, open_lower_bound=open_lower, open_upper_bound=open_upper,
                  zero_point=None, cdf_size=201, open_time=OPENED, scheduled_resolution_time=OPENED + timedelta(days=60))
    fields.update(kw)
    return NumericQuestion(**fields)


def discrete(**kw):
    fields = dict(question_text="How many will there be at the end?", id_of_post=8, page_url="https://www.metaculus.com/questions/8",
                  unit_of_measure="units", lower_bound=-0.5, upper_bound=10.5, open_lower_bound=False, open_upper_bound=False,
                  zero_point=None, cdf_size=12, open_time=OPENED, scheduled_resolution_time=OPENED + timedelta(days=60))
    fields.update(kw)
    return DiscreteQuestion(**fields)


def forecast(question, values):
    heights = (0.025, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.975)
    return mix_with_uniform(pchip_distribution([Percentile(percentile=h, value=v) for h, v in zip(heights, values)], question), question)


LIVE = (10, 11, 12, 14, 16, 19, 23, 26, 29)
WINDOW = (14, 15, 16, 18, 20, 23, 27, 30, 33)


@pytest.mark.parametrize("bounds", [(True, True), (False, False), (True, False)])
def test_blend_is_the_pointwise_half_and_half_and_platform_valid(bounds):
    q = numeric(*bounds)
    live, window = forecast(q, LIVE), forecast(q, WINDOW)
    blend = shadow.window_blend(live, to_jsonable(window), q)  # the window comes from the log (JSON form)
    assert blend is not None and distribution_problems(blend, q) == []
    expected = 0.5 * _declared_cdf(live, q) + 0.5 * _declared_cdf(window, q)
    assert _declared_cdf(blend, q) == pytest.approx(expected, abs=1e-6)


def test_blend_on_a_discrete_question():
    q = discrete()
    live, window = forecast(q, (1, 1.5, 2, 3, 4, 5, 6, 7, 8)), forecast(q, (2, 2.5, 3, 4, 5, 6, 7, 8, 9))
    blend = shadow.window_blend(live, to_jsonable(window), q)
    assert blend is not None and distribution_problems(blend, q) == []
    assert len(blend.declared_percentiles) == 12
    expected = 0.5 * _declared_cdf(live, q) + 0.5 * _declared_cdf(window, q)
    assert _declared_cdf(blend, q) == pytest.approx(expected, abs=1e-6)


def test_a_baseline_on_another_point_set_is_interpolated_onto_the_live_grid():
    q = numeric()
    live, window = forecast(q, LIVE), forecast(q, WINDOW)
    saved = to_jsonable(window)
    thinned = dict(saved, declared_percentiles=saved["declared_percentiles"][::4] + saved["declared_percentiles"][-1:])
    assert len(thinned["declared_percentiles"]) < 201
    on_grid = shadow._cdf_on_grid(thinned, q)
    assert len(on_grid) == 201 and np.all(np.diff(on_grid) >= 0)
    assert on_grid == pytest.approx(_declared_cdf(window, q), abs=5e-3)  # linear between the kept points
    assert shadow._cdf_on_grid(saved, q) == pytest.approx(_declared_cdf(window, q), abs=1e-12)  # same grid: exact
    blend = shadow.window_blend(live, thinned, q)
    assert blend is not None and distribution_problems(blend, q) == []


def test_a_blend_that_breaks_the_platform_rules_is_dropped(monkeypatch):
    q = numeric()
    monkeypatch.setattr(shadow, "_cdf_on_grid", lambda f, question: np.zeros(201))  # flat: steps below the minimum
    assert shadow.window_blend(forecast(q, LIVE), forecast(q, WINDOW), q) is None


def bot():
    return main.FallBot2026(
        llms={"default": "openrouter/x:free", "parser": "openrouter/x:free", "summarizer": "openrouter/x:free", "researcher": "no_research"},
        publish_reports_to_metaculus=False,
    )


def test_saved_only_where_a_window_baseline_exists():
    q = numeric()
    b, live = bot(), forecast(q, LIVE)
    record = {"shadow": {"mean-cdf": to_jsonable(live)}}
    b._window_blend_shadow(q, record, live)
    assert shadow.WINDOW_BLEND not in record["shadow"]
    record = {}
    b._window_blend_shadow(q, record, live)
    assert record == {}
    record = {"shadow": {"window": to_jsonable(forecast(q, WINDOW))}}
    b._window_blend_shadow(q, record, live)
    saved = record["shadow"]["window-blend"]["declared_percentiles"]
    assert len(saved) == 201 and all(b["percentile"] > a["percentile"] for a, b in zip(saved, saved[1:]))


def test_a_failure_is_only_logged_and_never_reaches_the_forecast(monkeypatch, caplog):
    def broken(*a, **k):
        raise ArithmeticError("boom")

    monkeypatch.setattr(main, "window_blend", broken)
    q = numeric()
    record = {"shadow": {"window": to_jsonable(forecast(q, WINDOW))}}
    with caplog.at_level("INFO"):
        bot()._window_blend_shadow(q, record, forecast(q, LIVE))
    assert shadow.WINDOW_BLEND not in record["shadow"]
    assert "window blend shadow failed (ArithmeticError)" in caplog.text


def test_scored_as_its_own_shadow_column_for_numeric_and_discrete():
    rows = []
    for kind, q, live_values, window_values, answer in (
        ("numeric", numeric(), LIVE, WINDOW, "21.0"),
        ("discrete", discrete(), (1, 1.5, 2, 3, 4, 5, 6, 7, 8), (2, 2.5, 3, 4, 5, 6, 7, 8, 9), "6"),
    ):
        live, window = forecast(q, live_values), forecast(q, window_values)
        record = {"question": {"id_of_post": q.id_of_post, "question_type": kind}, "final_forecast": to_jsonable(live),
                  "shadow": {"window": to_jsonable(window)}}
        bot()._window_blend_shadow(q, record, live)
        rows += scoreboard.score_records([record], {q.id_of_post: answer}.get)
    for row in rows:
        live, window, blend = row["live"], row["shadow"]["window"], row["shadow"]["window-blend"]
        # Mass is linear in the CDF: the blend's bucket mass is the average of the two.
        assert np.exp(blend) == pytest.approx(0.5 * np.exp(live) + 0.5 * np.exp(window), rel=1e-3)
    report = scoreboard.report_markdown(rows)
    assert "| numeric | window-blend | 1 |" in report and "| discrete | window-blend | 1 |" in report


def test_replay_pipeline_saves_the_blend_on_numeric_and_discrete_with_zero_calls(monkeypatch):
    """The replay Test Bot path (recorded replies, 0 model calls) with a stand-in
    official series, so the window baseline exists and the blend is built."""
    values = [16.0 + 4 * ((i % 50) / 50) for i in range(3000)]
    history = [[(date(2000, 1, 1) + timedelta(days=i)).isoformat(), v] for i, v in enumerate(values)]
    official = {"source": "fred", "series": "VIXCLS", "latest": {"date": "2026-09-30", "value": 16.0}, "history": history[-500:]}
    small = [[d, v / 4] for d, v in history]  # the discrete question's level (about 4-5)
    monkeypatch.setattr(main, "hard_data_for", lambda q: dict(official, latest={"date": "2026-09-30", "value": 16.0 if q.id_of_post == 7 else 4.0}))
    monkeypatch.setattr(main, "fetch_full_history", lambda f: history if f["latest"]["value"] == 16.0 else small)
    lineup = bot_config.get_lineup("replay")
    b = main.FallBot2026(llms=lineup.llms, publish_reports_to_metaculus=False, enable_summarize_research=False,
                         predictions_per_research_report=1)
    b.keep_records = True
    replay.ReplayLlm.calls = 0
    questions = [numeric(), discrete()]
    reports = asyncio.run(b.forecast_questions(questions, return_exceptions=True))
    assert not any(isinstance(r, BaseException) for r in reports), reports
    assert replay.ReplayLlm.calls == 2
    by_post = {r["question"]["id_of_post"]: r for r in b.kept_records}
    for q in questions:
        saved = by_post[q.id_of_post]["shadow"]
        assert "window" in saved and "window-blend" in saved, sorted(saved)
        assert len(saved["window-blend"]["declared_percentiles"]) == q.cdf_size
