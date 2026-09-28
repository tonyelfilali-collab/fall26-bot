"""Build 2a: rule-based matching to FRED / CoinGecko, and fetching (fake API replies)."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

import hard_data
import real_regression

EXPECTED = {41084: "BAMLH0A0HYM2", 41083: "DGS10", 41080: "VIXCLS", 39492: "CIVPART"}


def test_regression_pack_matches_only_the_clear_ones():
    numeric = [q for q in real_regression.load_questions() if q.question_type in ("numeric", "discrete")]
    assert len(numeric) == 21
    found = {q.id_of_post: m.series for q in numeric if (m := hard_data.match_question(q))}
    assert found == EXPECTED


def _q(text, kind="numeric"):
    return SimpleNamespace(question_type=kind, question_text=text)


@pytest.mark.parametrize(
    "text, series",
    [
        ("What will the US unemployment rate be for November 2026?", "UNRATE"),
        ("What will the price of Bitcoin be on December 31, 2026?", "bitcoin"),
        ("What will be the closing price of ETH on Nov 1?", "ethereum"),
        ("What will the WTI crude oil price be on Oct 15?", "DCOILWTICO"),
        ("What will the 10-year Treasury yield be on Nov 3?", "DGS10"),
    ],
)
def test_clear_matches(text, series):
    assert hard_data.match_question(_q(text)).series == series


@pytest.mark.parametrize(
    "text, kind",
    [
        ("Will Bitcoin trade above $100k?", "binary"),  # not numeric
        ("How much will Bitcoin's returns exceed Ethereum's?", "numeric"),  # a comparison
        ("How many Bitcoin ETFs will be approved?", "numeric"),  # no price word
        ("What will Trump's approval rating be?", "numeric"),
        ("What will unemployment be in Spain?", "numeric"),  # not the US rate
    ],
)
def test_no_match(text, kind):
    assert hard_data.match_question(_q(text, kind)) is None


def _get(payload, calls):
    def get(url, params=None, timeout=None):
        calls.append((url, params))
        return SimpleNamespace(json=lambda: payload, raise_for_status=lambda: None)

    return get


def test_fetch_fred_latest_and_history():
    calls = []
    payload = {"observations": [{"date": "2026-09-24", "value": "4.1"}, {"date": "2026-09-25", "value": "."},
                                {"date": "2026-09-26", "value": "4.2"}]}
    data = hard_data.fetch_fred("DGS10", date(2026, 9, 28), _get(payload, calls), api_key="k")
    assert data["latest"] == {"date": "2026-09-26", "value": 4.2}
    assert data["history"] == [["2026-09-24", 4.1], ["2026-09-26", 4.2]]
    assert calls[0][1]["series_id"] == "DGS10" and calls[0][1]["observation_start"] == "2024-09-28"


def test_fetch_coingecko():
    payload = {"prices": [[1790000000000, 60000.0], [1790086400000, 61000.5]]}
    data = hard_data.fetch_coingecko("bitcoin", _get(payload, []))
    assert data["latest"]["value"] == 61000.5 and len(data["history"]) == 2


def test_failure_is_saved_not_raised(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "test")

    def broken(*args, **kwargs):
        raise ConnectionError("down")

    found = hard_data.hard_data_for(_q("What will the VIX close at on Oct 1?"), date(2026, 9, 28), broken)
    assert found["series"] == "VIXCLS" and found["error"] == "ConnectionError" and "latest" not in found
    assert hard_data.hard_data_for(_q("How many seats?"), date(2026, 9, 28), broken) is None
    monkeypatch.delenv("FRED_API_KEY")
    assert hard_data.hard_data_for(_q("What will the VIX close at on Oct 1?"), date(2026, 9, 28), broken)["error"] == "KeyError"
