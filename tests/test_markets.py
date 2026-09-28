"""
Tests for PLAN.md Step 9 in log-only mode (markets.py). No network: fake
search results and a fake judge.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from forecasting_tools import BinaryQuestion, ReasonedPrediction

import main
import markets
from markets import Candidate

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def candidate(source="polymarket", volume=50_000.0, updated_hours_ago=2, **kw):
    fields = dict(
        source=source, market_id="m1", title="Will the Fed cut rates in October 2026?", rules="FOMC statement",
        deadline="2026-10-28", price=0.3, volume=volume,
        updated=(NOW - timedelta(hours=updated_hours_ago)).isoformat() if updated_hours_ago is not None else None,
        url="https://example.com",
    )
    fields.update(kw)
    return Candidate(**fields)


# ---------------------------------------------------------------- normalising sources


def test_manifold_normalising_skips_non_binary_and_resolved():
    data = [
        {"id": "a", "question": "Q1", "outcomeType": "BINARY", "probability": 0.2, "volume": 2000, "closeTime": 1793211900000, "lastUpdatedTime": 1790000000000, "url": "u"},
        {"id": "b", "question": "Q2", "outcomeType": "MULTIPLE_CHOICE"},
        {"id": "c", "question": "Q3", "outcomeType": "BINARY", "isResolved": True},
    ]
    [c] = markets.manifold_candidates(data)
    assert (c.source, c.price, c.volume) == ("manifold", 0.2, 2000.0)
    assert c.deadline.startswith("2026-10-28")


def test_polymarket_normalising_reads_the_yes_price_and_skips_closed():
    data = {"events": [{"slug": "fed", "markets": [
        {"id": "1", "question": "Fed cut in Oct?", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.31", "0.69"]', "volume": "250000", "active": True, "closed": False, "endDate": "2026-10-28T00:00:00Z", "updatedAt": "2026-09-28T10:00:00Z"},
        {"id": "2", "question": "Old", "active": True, "closed": True},
    ]}]}
    [c] = markets.polymarket_candidates(data)
    assert c.price == pytest.approx(0.31) and c.volume == 250000


def test_kalshi_normalising_uses_the_bid_ask_midpoint():
    data = {"current_page": [{"event_title": "Fed rate cut before 2027?", "series_ticker": "KXRATECUT", "markets": [
        {"ticker": "KX-1", "yes_bid": 4, "yes_ask": 6, "last_price": 4, "volume": 2735735, "close_ts": "2027-01-01T04:59:00Z", "yes_subtitle": "Cuts"},
    ]}]}
    [c] = markets.kalshi_candidates(data)
    assert c.price == pytest.approx(0.05) and c.title == "Fed rate cut before 2027? Cuts"


# ---------------------------------------------------------------- checks and the judge


def test_checks_fresh_and_liquid():
    assert markets.apply_checks(candidate(), NOW).fresh is True
    assert markets.apply_checks(candidate(updated_hours_ago=30), NOW).fresh is False
    assert markets.apply_checks(candidate(updated_hours_ago=None), NOW).fresh is False
    assert markets.apply_checks(candidate(volume=5_000), NOW).liquid is False  # polymarket needs 10,000


def test_accepted_only_if_all_three_yes_and_fresh_and_liquid():
    cands = [markets.apply_checks(candidate(), NOW) for _ in range(4)]
    cands[3] = markets.apply_checks(candidate(volume=10), NOW)
    verdicts = json.dumps([
        {"index": 0, "same_event": True, "same_source": True, "same_deadline": True, "reason": "identical"},
        {"index": 1, "same_event": True, "same_source": True, "same_deadline": False, "reason": "different date"},
        {"index": 2, "same_event": True, "same_source": False, "same_deadline": True, "reason": "other source"},
        {"index": 3, "same_event": True, "same_source": True, "same_deadline": True, "reason": "identical but thin"},
    ])
    result = markets.apply_verdicts(cands, "Here you go: " + verdicts)
    assert [c.accepted for c in result] == [True, False, False, False]
    assert result[1].reason == "different date"


def test_bad_judge_answer_accepts_nothing():
    cands = [markets.apply_checks(candidate(), NOW)]
    assert markets.apply_verdicts(cands, "not json")[0].accepted is False


def test_match_question_mixes_sources_and_caps():
    found = [candidate(source="manifold", volume=5000) for _ in range(4)] + [candidate(source="polymarket") for _ in range(3)] + [candidate(source="kalshi", volume=5000)]

    async def judge(prompt):
        return json.dumps([{"index": i, "same_event": True, "same_source": True, "same_deadline": True} for i in range(5)])

    q = SimpleNamespace(question_text="Will the Fed cut rates in October 2026?", resolution_criteria="", close_time=None, scheduled_resolution_time=None)
    result = asyncio.run(markets.match_question(q, judge, search=lambda query: found))
    assert [c.source for c in result] == ["polymarket", "polymarket", "kalshi", "manifold", "manifold"]


def test_markdown_table():
    table = markets.markdown_table([(123, markets.apply_checks(candidate(accepted=True), NOW))])
    assert "| 1 | 123 | polymarket |" in table and "30%" in table


# ---------------------------------------------------------------- log-only in the bot: forecast unchanged


def _bot():
    return main.FallBot2026(
        llms={"default": "openrouter/x:free", "parser": "openrouter/x:free", "summarizer": "openrouter/x:free", "researcher": "no_research"},
        publish_reports_to_metaculus=False,
        enable_summarize_research=False,
    )


class _Writer:
    def __init__(self):
        self.records = []

    def save(self, path, record):
        self.records.append(record)
        return True


def _forecast(monkeypatch, market_mode, candidates_found):
    async def fake_forecast(self, question, prompt):
        return ReasonedPrediction(prediction_value=0.42, reasoning="Probability: 42%")

    async def fake_match(question, invoke_judge):
        return candidates_found

    monkeypatch.setattr(main.FallBot2026, "_binary_prompt_to_forecast", fake_forecast)
    monkeypatch.setattr(main, "match_question", fake_match)
    monkeypatch.setattr(main, "MARKET_MODE", market_mode)
    bot = _bot()
    bot.question_log = _Writer()
    q = BinaryQuestion(question_text="Will X?", id_of_post=5, page_url="https://www.metaculus.com/questions/5")
    [report] = asyncio.run(bot.forecast_questions([q], return_exceptions=True))
    return report, bot.question_log.records[0]


def test_log_only_saves_candidates_but_never_changes_the_forecast(monkeypatch):
    found = [markets.apply_checks(candidate(price=0.9, accepted=True), NOW)]
    with_markets, record = _forecast(monkeypatch, "log-only", found)
    without_markets, record_off = _forecast(monkeypatch, "off", found)
    assert with_markets.prediction == without_markets.prediction == pytest.approx(0.42)
    assert record["market_candidates"][0]["price"] == 0.9
    assert "market_candidates" not in record_off


def test_market_failure_never_breaks_the_forecast(monkeypatch):
    async def broken(question, invoke_judge):
        raise ConnectionError("down")

    async def fake_forecast(self, question, prompt):
        return ReasonedPrediction(prediction_value=0.42, reasoning="x")

    monkeypatch.setattr(main.FallBot2026, "_binary_prompt_to_forecast", fake_forecast)
    monkeypatch.setattr(main, "match_question", broken)
    bot = _bot()
    q = BinaryQuestion(question_text="Will X?", id_of_post=6, page_url="https://www.metaculus.com/questions/6")
    [report] = asyncio.run(bot.forecast_questions([q], return_exceptions=True))
    assert not isinstance(report, BaseException)
