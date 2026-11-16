"""Typical change dossier line (architect, 16 Nov): "Typical change over <horizon>:
10-90% range of past moves = <low> to <high> (<n> past periods)", from the
window baseline's method. Fake series, no network."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from forecasting_tools import NumericQuestion

import window_baseline
from tests.test_official_line import _bot, _research_then_parse, fred, numeric
from tests.test_official_line import RIGHT

START = date(2025, 1, 1)


def daily(values):
    return [[(START + timedelta(days=i)).isoformat(), v] for i, v in enumerate(values)]


def question(resolve: date, text="What will the 10-year Treasury yield be?", **kwargs) -> NumericQuestion:
    q = numeric(text, "Resolves to FRED series DGS10.", **kwargs)
    q.scheduled_resolution_time = datetime(resolve.year, resolve.month, resolve.day, tzinfo=timezone.utc)
    return q


def test_change_series_absolute_moves():
    # A rate that goes up 0.01 a day, every day: every 7-day move is +0.07.
    history = daily([4.0 + 0.01 * i for i in range(200)])
    found = fred(history=history)
    latest = date.fromisoformat(history[-1][0])
    line = window_baseline.typical_change_line(question(latest + timedelta(days=7)), found)
    assert line.startswith("Typical change over 7 days: 10-90% range of past moves = +0.07 to +0.07 (")
    assert line.endswith(" past periods).")


def test_ratio_series_percent_moves_and_the_10_90_range():
    # A coin alternating +10% / -10% around 100: 1-day moves of +10% or -9.1% (ratio).
    values = [100.0 if i % 2 == 0 else 110.0 for i in range(120)]
    found = {"source": "coingecko", "series": "bitcoin", "title": "bitcoin price (USD)",
             "latest": {"date": daily(values)[-1][0], "value": values[-1]}, "history": daily(values)}
    latest = date.fromisoformat(found["latest"]["date"])
    line = window_baseline.typical_change_line(question(latest + timedelta(days=1)), found)
    assert "over 1 day:" in line and "-9.1% to +10.0%" in line


def test_n_counts_the_windows_used():
    history = daily([4.0 + 0.001 * i for i in range(100)])
    latest = date.fromisoformat(history[-1][0])
    sample, _ = window_baseline.window_sample(history, 10, "end", history[-1][1], False)
    line = window_baseline.typical_change_line(question(latest + timedelta(days=10)), fred(history=history))
    assert f"({len(sample)} past periods)" in line


@pytest.mark.parametrize("found, days", [
    (None, 7),
    ({"source": "fred", "series": "DGS10", "error": "HTTPError"}, 7),  # fetch failed
    ("short", 7),  # under 30 past periods
    ("long", 0),  # resolves on the latest data date
])
def test_no_line(found, days):
    if found == "short":
        found = fred(history=daily([4.0] * 20))
    elif found == "long":
        found = fred(history=daily([4.0] * 200))
    latest = date.fromisoformat(found["latest"]["date"]) if found and "latest" in found else date(2026, 11, 16)
    assert window_baseline.typical_change_line(question(latest + timedelta(days=days)), found) is None


def test_line_goes_into_the_dossier_after_the_official_line(monkeypatch):
    history = daily([4.0 + 0.01 * i for i in range(200)])
    latest = date.fromisoformat(history[-1][0])
    q = question(latest + timedelta(days=7))
    bot = _bot(monkeypatch, [RIGHT])
    text, _ = _research_then_parse(bot, q, fred(history=history))
    first, second, rest = text.split("\n", 2)
    assert first.startswith("OFFICIAL DATA (FRED DGS10")
    assert second.startswith("Typical change over 7 days:")
    assert rest.strip() == "Dossier text."
    record = bot._record_for(q)
    assert record["official_data"]["typical_change"] == second


def test_no_typical_line_keeps_the_dossier_as_before(monkeypatch):
    q = question(date(2026, 12, 31))
    bot = _bot(monkeypatch, [RIGHT])
    text, _ = _research_then_parse(bot, q, fred())  # 6 points of history: under 30 periods
    assert text.split("\n")[1] == "" and "Typical change" not in text
    assert "typical_change" not in bot._record_for(q)["official_data"]
