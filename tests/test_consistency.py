"""Related-question consistency shadow: sibling ladders, direction, isotonic. No calls."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import consistency as c
from gemini_budget import MemoryStore


def _group(titles):
    groups = c.find_groups(dict(enumerate(titles)))
    assert len(groups) == 1, groups
    return groups[0]


def test_threshold_ladder_higher_is_harder():
    g = _group([
        "Will Bitcoin close above $200,000 on December 31, 2026?",
        "Will Bitcoin close above $100,000 on December 31, 2026?",
        "Will Bitcoin close above $150,000 on December 31, 2026?",
    ])
    assert g.kind == "n" and g.direction == "decreasing"
    assert [k for k, _ in g.members] == [1, 2, 0]  # sorted by threshold


def test_threshold_ladder_with_suffixes_and_lower_is_harder():
    g = _group(["Will US unemployment be below 4.5% in November 2026?", "Will US unemployment be below 4% in November 2026?"])
    assert g.direction == "increasing" and [v for _, v in g.members] == [(4.0,), (4.5,)]
    g = _group(["Will OpenAI exceed 1.5B weekly users by 2027?", "Will OpenAI exceed 900M weekly users by 2027?"])
    assert g.direction == "decreasing" and [v for _, v in g.members] == [(9e8,), (1.5e9,)]


def test_date_ladder_later_deadline_never_lower():
    g = _group([
        "Will the Fed cut rates by December 2026?",
        "Will the Fed cut rates by October 2026?",
        "Will the Fed cut rates by November 2026?",
    ])
    assert g.kind == "d" and g.direction == "increasing"
    assert [k for k, _ in g.members] == [1, 2, 0]


def test_date_on_a_day_has_no_direction():
    g = _group(["Will it rain in London on October 3, 2026?", "Will it rain in London on October 4, 2026?"])
    assert g.direction == "none"
    adjusted, bad = c.consistent_forecasts(g, {0: 0.6, 1: 0.2})
    assert adjusted == {0: 0.6, 1: 0.2} and bad == 0


@pytest.mark.parametrize(
    "titles",
    [
        # Different entity, same number: not siblings.
        ["Will Tesla stock close above $300 on Dec 31, 2026?", "Will Apple stock close above $300 on Dec 31, 2026?"],
        # Two things change (threshold AND date): not a clean ladder.
        ["Will Bitcoin close above $100,000 by June 2027?", "Will Bitcoin close above $150,000 by July 2027?"],
        # Same numbers, different wording.
        ["Will the Fed cut rates by 50 bps in December 2026?", "Will the Fed hike rates by 50 bps in December 2026?"],
        # Identical titles: nothing varies.
        ["Will X exceed 100 in 2026?", "Will X exceed 100 in 2026?"],
    ],
)
def test_similar_looking_non_siblings(titles):
    assert c.find_groups(dict(enumerate(titles))) == []


def test_isotonic_fixes_only_violations():
    assert c.isotonic([0.2, 0.4, 0.6], increasing=True) == [0.2, 0.4, 0.6]
    assert c.isotonic([0.2, 0.6, 0.4], increasing=True) == pytest.approx([0.2, 0.5, 0.5])
    # Decreasing: a higher threshold got a higher probability -> pooled.
    assert c.isotonic([0.7, 0.3, 0.5], increasing=False) == pytest.approx([0.7, 0.4, 0.4])
    assert c.violations([0.7, 0.3, 0.5], increasing=False) == 1


def test_consistent_forecasts_threshold_ladder():
    g = _group(["Will X exceed 100 in 2026?", "Will X exceed 200 in 2026?", "Will X exceed 300 in 2026?"])
    adjusted, bad = c.consistent_forecasts(g, {0: 0.8, 1: 0.3, 2: 0.4})
    assert bad == 1
    assert adjusted[0] == pytest.approx(0.8) and adjusted[1] == pytest.approx(0.35) and adjusted[2] == pytest.approx(0.35)


def test_index_and_shadow_for_open_siblings_only():
    index = c.ForecastIndex(MemoryStore({}))
    soon = datetime.now(timezone.utc) + timedelta(days=5)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    index.put(1, "Will X exceed 100 in 2026?", soon, 0.3, "seasonal")
    index.put(2, "Will X exceed 200 in 2026?", soon, 0.5, "seasonal")  # violation
    index.put(3, "Will X exceed 300 in 2026?", past, 0.9, "seasonal")  # closed: ignored
    index.put(4, "Will X exceed 400 in 2026?", soon, 0.9, "minibench")  # other tournament
    shadow = c.shadow_for(index, 2, "seasonal")
    assert shadow["group"] == ["1", "2"] and shadow["direction"] == "decreasing"
    assert shadow["violations"] == 1 and shadow["consistent"] == pytest.approx(0.4)
    assert c.shadow_for(index, 4, "minibench") is None  # no siblings there
    index.save()
    assert "3" not in index.data  # closed questions are dropped


def test_bot_saves_the_consistent_shadow_after_submission(monkeypatch):
    import asyncio

    from forecasting_tools import BinaryQuestion

    import main

    bot = main.FallBot2026(
        llms={"default": "openrouter/x:free", "parser": "openrouter/x:free", "summarizer": "openrouter/x:free", "researcher": "no_research"},
        publish_reports_to_metaculus=False,
    )
    bot.consistency_index = c.ForecastIndex(MemoryStore({}))
    soon = datetime.now(timezone.utc) + timedelta(days=5)
    q1 = BinaryQuestion(question_text="Will X exceed 100 in 2026?", id_of_post=11, page_url="https://www.metaculus.com/questions/11", close_time=soon)
    q2 = BinaryQuestion(question_text="Will X exceed 200 in 2026?", id_of_post=12, page_url="https://www.metaculus.com/questions/12", close_time=soon)
    r1, r2 = {}, {}
    bot._consistency_shadow(q1, r1, 0.3)
    assert "consistency" not in r1  # no sibling yet
    bot._consistency_shadow(q2, r2, 0.5)  # a higher threshold got a higher probability
    assert r2["consistency"]["violations"] == 1
    assert r2["shadow"]["consistent"] == pytest.approx(0.4)
