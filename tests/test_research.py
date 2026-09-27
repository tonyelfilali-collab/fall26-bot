"""
Tests for PLAN.md Step 7 research (research.py). Fake searches and a fake
helper model: no network, no real model calls.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

import research
from free_news import Article

NOW = datetime.now(timezone.utc)
QUESTION = SimpleNamespace(
    question_text="Will the Fed cut rates in October 2026?",
    resolution_criteria="Resolves Yes if the FOMC lowers the target range at its October meeting.",
    id_of_post=9,
)


def asknews_article(title: str, days_ago: int = 1):
    return SimpleNamespace(eng_title=title, summary=f"Summary of {title}", pub_date=NOW - timedelta(days=days_ago), source_id="reuters")


def free_article(title: str, days_ago: int = 1):
    return Article(published=NOW - timedelta(days=days_ago), source="Example", headline=title)


class FakeHelper:
    """Answers the planner with a plan and the dossier writer with a dossier."""

    def __init__(self, plan=None, dossier="## Current status\nRates at 4%.\nMISSING: none", fail=False):
        self.plan = plan if plan is not None else {"queries": ["Fed rate decision", "FOMC October", "inflation"], "key_facts": ["latest FOMC statement"]}
        self.dossier = dossier
        self.fail = fail
        self.prompts: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("helper down")
        if "plan news research" in prompt:
            return json.dumps(self.plan)
        return self.dossier


def payment_required():
    request = httpx.Request("POST", "https://api.asknews.app/v1/news/search")
    return httpx.HTTPStatusError("402", request=request, response=httpx.Response(402, request=request))


def run(helper, asknews=None, free=None):
    calls = {"asknews": [], "free": []}

    async def fake_asknews(query):
        calls["asknews"].append(query)
        if isinstance(asknews, Exception):
            raise asknews
        return asknews(query) if callable(asknews) else [asknews_article(f"{query} story {i}") for i in range(4)]

    def fake_free(query):
        calls["free"].append(query)
        return free(query) if callable(free) else [free_article(f"{query} free {i}", i + 1) for i in range(3)]

    result = asyncio.run(research.run_planned_research(QUESTION, "Today is 2026-09-28.", helper, fake_asknews, fake_free))
    return result, calls


# ---------------------------------------------------------------- planning


def test_parse_plan_reads_json_and_caps_queries():
    plan = research.parse_plan('Sure! {"queries": ["a", "b", "c", "d"], "key_facts": ["x"]}', "fallback")
    assert plan.queries == ["a", "b", "c"] and plan.key_facts == ["x"]


def test_parse_plan_falls_back_to_the_question():
    assert research.parse_plan("no json here", "the question").queries == ["the question"]


# ---------------------------------------------------------------- AskNews budget


def test_asknews_at_most_3_calls_per_question():
    result, calls = run(FakeHelper(dossier="## Current status\nx\nMISSING: FOMC dot plot"))
    assert len(calls["asknews"]) <= 3
    assert result.asknews_calls <= 3


def test_one_asknews_call_is_kept_for_the_gap_fill():
    result, calls = run(FakeHelper(dossier="## Current status\nx\nMISSING: FOMC dot plot"))
    assert calls["asknews"] == ["Fed rate decision", "FOMC October", "FOMC dot plot"]
    assert result.gap_filled and "Extra search: FOMC dot plot" in result.dossier


def test_no_gap_fill_when_nothing_is_missing():
    result, calls = run(FakeHelper())
    assert calls["asknews"] == ["Fed rate decision", "FOMC October"]
    assert not result.gap_filled


def test_third_query_used_when_the_first_two_find_too_little():
    result, calls = run(FakeHelper(), asknews=lambda q: [asknews_article(q)] if q == "Fed rate decision" else [])
    assert calls["asknews"] == ["Fed rate decision", "FOMC October", "inflation"]


# ---------------------------------------------------------------- free news fallback


def test_asknews_402_falls_back_to_free_news_for_every_query():
    result, calls = run(FakeHelper(), asknews=payment_required())
    assert calls["asknews"] == ["Fed rate decision"]  # stops after the first failure
    assert calls["free"] == ["Fed rate decision", "FOMC October", "inflation"]
    assert result.free_articles > 0 and result.asknews_articles == 0


def test_too_few_asknews_articles_are_topped_up_with_free_news():
    result, calls = run(FakeHelper(), asknews=lambda q: [asknews_article(q)] if q == "Fed rate decision" else [])
    assert result.asknews_articles == 1 and result.free_articles > 0


def test_nothing_found_anywhere_does_not_crash():
    result, _ = run(FakeHelper(dossier="MISSING: none"), asknews=lambda q: [], free=lambda q: [])
    assert result.articles == 0
    assert result.dossier  # the "no articles" text, never empty


def test_helper_down_uses_the_articles_directly():
    result, _ = run(FakeHelper(fail=True))
    assert not result.dossier_written
    assert result.queries == [QUESTION.question_text]
    assert "story" in result.dossier


# ---------------------------------------------------------------- dossier


def test_dossier_has_no_market_prices():
    dossier = "## Current status\nRates at 4%.\nPolymarket gives 70%.\nKalshi odds: 65%.\nMISSING: none"
    result, _ = run(FakeHelper(dossier=dossier))
    assert "Polymarket" not in result.dossier and "Kalshi" not in result.dossier
    assert "Rates at 4%" in result.dossier


def test_dossier_is_capped_at_6000_tokens():
    long = "## Current status\n" + ("word " * 10000) + "\nMISSING: none"
    result, _ = run(FakeHelper(dossier=long))
    assert len(result.dossier.split()) <= int(6000 * 0.75) + 1


def test_dossier_prompt_asks_for_the_sections_and_dates():
    helper = FakeHelper()
    run(helper)
    dossier_prompt = helper.prompts[1]
    for section in ("Current status", "What must happen", "Base rates", "Key uncertainties"):
        assert section in dossier_prompt
    assert "Today is 2026-09-28." in dossier_prompt
    assert "Do NOT mention prediction markets" in dossier_prompt


def test_split_missing():
    assert research.split_missing("body\nMISSING: none") == ("body", None)
    assert research.split_missing("body\nMISSING: 'fed dot plot'") == ("body", "fed dot plot")
    assert research.split_missing("no missing line") == ("no missing line", None)
