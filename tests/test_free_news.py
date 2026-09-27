"""
Tests for the free news fallback (Google News RSS + GDELT). No network: the
fetchers and AskNews are replaced with fakes.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from forecasting_tools import BinaryQuestion

import free_news
import main
from free_news import (
    Article,
    build_query,
    count_asknews_articles,
    format_articles,
    parse_gdelt_json,
    parse_google_news_rss,
    select_articles,
)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def article(days_ago: float, headline: str = "", snippet: str = "") -> Article:
    return Article(
        published=NOW - timedelta(days=days_ago),
        source="Example News",
        headline=headline or f"Headline {days_ago} days ago",
        snippet=snippet,
    )


# ---------------------------------------------------------------- parsing

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Rates held steady - Reuters</title><pubDate>Fri, 25 Sep 2026 10:00:00 GMT</pubDate>
<description>&lt;a href="x"&gt;Rates held steady&lt;/a&gt; Reuters</description><source url="https://reuters.com">Reuters</source></item>
<item><title>No date here</title><source>AP</source></item>
</channel></rss>"""

GDELT = """{"articles": [
 {"title": "Parliament votes on budget", "seendate": "20260926T083000Z", "domain": "example.org"},
 {"title": "Bad date", "seendate": "yesterday", "domain": "x.org"}]}"""


def test_parse_google_news_rss():
    [item] = parse_google_news_rss(RSS)
    assert item.headline == "Rates held steady"  # " - Reuters" suffix removed
    assert item.source == "Reuters"
    assert item.published == datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)
    assert item.snippet == ""  # description only repeated the headline


def test_parse_gdelt_json():
    [item] = parse_gdelt_json(GDELT)
    assert item.headline == "Parliament votes on budget"
    assert item.source == "example.org"
    assert item.published == datetime(2026, 9, 26, 8, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("bad", ["", "not xml", "<rss></rss>"])
def test_empty_or_broken_rss_gives_no_articles(bad):
    assert parse_google_news_rss(bad) == []


@pytest.mark.parametrize("bad", ["", "Please limit requests to one every 5 seconds", "{}", "[]"])
def test_empty_or_broken_gdelt_gives_no_articles(bad):
    assert parse_gdelt_json(bad) == []


def test_build_query_keeps_keywords_acronyms_and_numbers():
    assert build_query("Will any founding member of the EU trigger Article 50 before 2027?") == (
        "founding member EU trigger Article 50"
    )
    assert build_query("?") == ""


# ---------------------------------------------------------------- dated and capped


def test_research_is_recent_newest_first_deduplicated_and_capped():
    articles = [article(d) for d in (5, 1, 70, 30, 59)] + [article(2, "Same story"), article(3, "Same story!")]
    articles += [article(10 + i, f"Story {i}") for i in range(10)]
    kept = select_articles(articles, now=NOW)
    assert len(kept) == 10
    assert all(NOW - a.published <= timedelta(days=60) for a in kept)  # 70-day-old one dropped
    assert [a.published for a in kept] == sorted((a.published for a in kept), reverse=True)
    assert sum(a.headline.startswith("Same story") for a in kept) == 1


def test_research_text_is_dated_and_word_capped():
    long_snippet = "word " * 500
    text = format_articles([article(d, snippet=long_snippet) for d in range(10)], max_words=2000)
    lines = text.splitlines()[1:]
    assert len(text.split()) <= 2000
    assert all(line.startswith("- 2026-") for line in lines)  # every article dated
    assert all(len(line.split()) <= 100 for line in lines)  # snippets are trimmed to 80 words
    assert format_articles([]) == ""


def test_count_asknews_articles():
    assert count_asknews_articles("**A**\nPublish date: x\n\n**B**\nPublish date: y") == 2
    assert count_asknews_articles("") == 0


# ---------------------------------------------------------------- fallback in the bot


def _bot() -> main.FallBot2026:
    return main.FallBot2026(
        llms={"default": "openrouter/x:free", "parser": "openrouter/x:free", "summarizer": "openrouter/x:free", "researcher": "asknews/news-summaries"},
        publish_reports_to_metaculus=False,
    )


def _payment_required() -> Exception:
    request = httpx.Request("POST", "https://api.asknews.app/v1/news/search")
    response = httpx.Response(402, request=request)
    return httpx.HTTPStatusError("402 Payment Required", request=request, response=response)


QUESTION = BinaryQuestion(question_text="Will the Fed cut rates in October 2026?", id_of_post=7)


def _run(monkeypatch, asknews, free_articles, caplog):
    calls = {"free": 0}

    async def fake_asknews(self, preset, prompt):
        if isinstance(asknews, Exception):
            raise asknews
        return asknews

    def fake_free(title):
        calls["free"] += 1
        return free_articles

    monkeypatch.setenv("ASKNEWS_API_KEY", "dummy")
    monkeypatch.delenv("ASKNEWS_CLIENT_ID", raising=False)
    monkeypatch.setattr(main.AskNewsSearcher, "call_preconfigured_version", fake_asknews)
    monkeypatch.setattr(main, "collect_free_news", fake_free)
    with caplog.at_level(logging.INFO, logger="fall26"):
        research = asyncio.run(_bot().run_research(QUESTION))
    return research, calls["free"]


def test_asknews_402_falls_back_to_free_news(monkeypatch, caplog):
    research, free_calls = _run(monkeypatch, _payment_required(), [article(1, "Fed signals cut")], caplog)
    assert free_calls == 1
    assert "Fed signals cut" in research and "2026-09-26" in research
    assert "AskNews failed (HTTP 402)" in caplog.text
    assert "articles found: 1 (AskNews 0, free news 1)" in caplog.text
    assert "Fed signals cut" not in caplog.text  # the log never shows article text


def test_no_articles_anywhere_does_not_crash(monkeypatch, caplog):
    research, _ = _run(monkeypatch, _payment_required(), [], caplog)
    assert research == "No recent news articles were found."
    assert "articles found: 0" in caplog.text


def test_asknews_with_enough_articles_skips_free_news(monkeypatch, caplog):
    asknews_text = "".join(f"**A{i}**\nPublish date: x\n\n" for i in range(3))
    research, free_calls = _run(monkeypatch, asknews_text, [article(1)], caplog)
    assert free_calls == 0
    assert research == asknews_text
    assert "(AskNews 3, free news 0)" in caplog.text


def test_asknews_with_few_articles_is_topped_up(monkeypatch, caplog):
    asknews_text = "**A0**\nPublish date: x\n\n"
    research, free_calls = _run(monkeypatch, asknews_text, [article(1, "Extra story")], caplog)
    assert free_calls == 1
    assert "A0" in research and "Extra story" in research


def test_free_news_source_failure_is_contained(monkeypatch):
    def boom(query):
        raise ConnectionError("down")

    monkeypatch.setattr(free_news, "fetch_google_news", boom)
    monkeypatch.setattr(free_news, "fetch_gdelt", boom)
    assert free_news.collect_free_news("Will the Fed cut rates?") == []
