"""Build 6: Wikipedia background (6a) and Yahoo Finance data (6b). Fake replies, no calls."""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

import hard_data
import research
import wikipedia


def _reply(status, body):
    return SimpleNamespace(status_code=status, json=lambda: body, raise_for_status=lambda: None)


# ---------------------------------------------------------------- 6a Wikipedia


def test_background_caps_skips_failures_and_disambiguation():
    pages = {
        "Federal_Reserve": _reply(200, {"title": "Federal Reserve", "extract": "word " * 500}),
        "Jerome_Powell": _reply(200, {"title": "Jerome Powell", "extract": "powell " * 500}),
        "Mercury": _reply(200, {"type": "disambiguation", "extract": "may refer to"}),
    }

    def get(url, headers=None, timeout=None):
        assert "fall26-bot" in headers["User-Agent"]
        key = url.rsplit("/", 1)[1]
        if key == "Broken":
            raise ConnectionError("down")
        return pages.get(key, _reply(404, {}))

    section, titles = wikipedia.background(["Broken", "Mercury", "Federal Reserve", "Jerome Powell"], date(2026, 9, 29), get)
    assert titles == ["Federal Reserve"]  # the 3-entity limit: Broken, Mercury, Federal Reserve
    assert section.startswith("## Background (Wikipedia, retrieved 2026-09-29)")
    assert len(section.split()) <= int(wikipedia.MAX_TOKENS * wikipedia.WORDS_PER_TOKEN) + 12
    both, titles = wikipedia.background(["Federal Reserve", "Jerome Powell"], date(2026, 9, 29), get)
    assert titles == ["Federal Reserve", "Jerome Powell"]
    assert len(both.split()) <= int(wikipedia.MAX_TOKENS * wikipedia.WORDS_PER_TOKEN) + 12  # 800 tokens in all
    assert wikipedia.background(["Broken"], date(2026, 9, 29), get) == ("", [])


def test_planner_names_entities():
    plan = research.parse_plan(json.dumps({"queries": ["q"], "key_facts": [], "entities": ["A", "B", "C", "D"]}), "x")
    assert plan.entities == ["A", "B", "C"]
    assert "Wikipedia" in research.planner_prompt("Q?", "", "Today")


def _run_research(fetch_background, dossier_words=5000):
    async def helper(prompt):
        if "plan news research" in prompt:
            return json.dumps({"queries": ["q"], "key_facts": [], "entities": ["Federal Reserve"]})
        return "## Status\\n" + "d " * dossier_words + "\\nMISSING: none"

    async def asknews(query):
        return []

    q = SimpleNamespace(question_text="Will the Fed cut?", resolution_criteria="", id_of_post=1, question_type="binary")
    return asyncio.run(research.run_planned_research(q, "Today", helper, asknews, lambda query: [], fetch_background))


def test_dossier_gets_the_background_and_stays_within_6k():
    result = _run_research(lambda entities: ("## Background (Wikipedia, retrieved 2026-09-29)\n" + "w " * 590, ["Federal Reserve"]))
    assert result.wikipedia == ["Federal Reserve"]
    assert "## Background (Wikipedia" in result.dossier
    assert len(result.dossier.split()) <= int(research.MAX_DOSSIER_TOKENS * research.WORDS_PER_TOKEN)


def test_wikipedia_failure_means_no_background():
    def broken(entities):
        raise ConnectionError("down")

    result = _run_research(broken, dossier_words=100)
    assert result.wikipedia == [] and "Background (Wikipedia" not in result.dossier


# ---------------------------------------------------------------- 6b Yahoo Finance


def _q(text):
    return SimpleNamespace(question_type="numeric", question_text=text)


@pytest.mark.parametrize(
    "text, source, series",
    [
        ("What will the price of gold be on December 31, 2026?", "yahoo", "GC=F"),
        ("What will the Nikkei 225 close at on Oct 30?", "yahoo", "^N225"),
        ("What will the closing price of Coca-Cola (NYSE: KO) be on Nov 1?", "yahoo", "KO"),
        ("What will the EUR/USD exchange rate be on Dec 1?", "yahoo", "EURUSD=X"),
        ("What will Nvidia's stock price be at the end of October?", "yahoo", "NVDA"),
        ("What will the S&P 500 closing value be on Dec 31?", "fred", "SP500"),  # FRED first
    ],
)
def test_yahoo_matches(text, source, series):
    m = hard_data.match_question(_q(text))
    assert (m.source, m.series) == (source, series)


@pytest.mark.parametrize("text", ["How many gold medals will the US win?", "How many Apple employees will there be?"])
def test_yahoo_non_matches(text):
    assert hard_data.match_question(_q(text)) is None


def test_yahoo_switch_off(monkeypatch):
    monkeypatch.setattr(hard_data, "YAHOO_ENABLED", False)
    assert hard_data.match_question(_q("What will the price of gold be?")) is None


def test_fetch_yahoo_and_the_line():
    t0 = int(datetime(2026, 8, 28, 20, tzinfo=timezone.utc).timestamp())
    body = {"chart": {"result": [{"timestamp": [t0, t0 + 86400 * 30, t0 + 86400 * 31],
                                  "indicators": {"quote": [{"close": [2500.0, None, 2600.5]}]}}]}}
    calls = []

    def get(url, params=None, headers=None, timeout=None):
        calls.append((url, params))
        return _reply(200, body)

    data = hard_data.fetch_yahoo("GC=F", get)
    assert calls[0][0].endswith("/chart/GC=F") and calls[0][1]["range"] == "1y"
    assert data["latest"]["value"] == 2600.5 and len(data["history"]) == 2  # the missing close is dropped
    question = SimpleNamespace(question_type="numeric", question_text="What will the price of gold be?",
                               resolution_criteria="Per the LBMA gold price (spot).", fine_print="",
                               lower_bound=1000.0, upper_bound=5000.0, nominal_lower_bound=None, nominal_upper_bound=None)
    found = {"source": "yahoo", "series": "GC=F", "title": "Gold futures (COMEX, USD/oz)", "rule": "gold", **data}
    line = hard_data.official_line(question, found)
    assert line.text.startswith("OFFICIAL DATA (Yahoo Finance GC=F")
    assert not line.exact and "futures price; the question asks about a spot price" in line.text


def test_yahoo_blocked_is_saved_not_raised():
    def blocked(url, params=None, headers=None, timeout=None):
        response = SimpleNamespace(status_code=429)
        raise hard_data.requests.HTTPError("Too Many Requests", response=response)

    found = hard_data.hard_data_for(_q("What will the price of gold be?"), get=blocked)
    assert found["source"] == "yahoo" and found["error"] == "HTTPError (HTTP 429)"
    assert hard_data.official_line(_q("x"), found) is None  # no line
