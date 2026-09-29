"""
Build 2a: hard data for numeric questions, from official free APIs only.

- FRED (St. Louis Fed; secret FRED_API_KEY) and CoinGecko's public API (no key).
- A rule-based matcher (no model call) maps a numeric or discrete question to
  one FRED series or one coin when the question clearly names it, and
  otherwise does nothing.
- For a match it fetches the latest value, its date, and about 2 years of
  history (CoinGecko's public API: 1 year), saved in the question log
  ("hard_data").
- Build 2c, partial (architect, 29 Sep): official_line() makes ONE dossier
  line (latest value and date, 1-year min/max, 30-day change), with a
  warning when the series is only a stand-in for what the question asks.
  Only an exact match's latest value goes to the unit check. The random-walk
  range never goes into the dossier (it stays a shadow).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable

import requests

from bot_helpers import PUBLIC_LOGGER_NAME

logger = logging.getLogger(PUBLIC_LOGGER_NAME)

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/{coin}/market_chart"
HISTORY_DAYS = 730
COIN_HISTORY_DAYS = 365  # the public CoinGecko API's limit
TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class Match:
    source: str  # "fred" or "coingecko"
    series: str  # FRED series id or CoinGecko coin id
    title: str
    rule: str  # the words that matched, for checking


# (regex on the question text, FRED series id, title). Order matters: the
# first match wins, so specific rules come before general ones. Each rule
# needs the exact quantity named, never just a topic.
FRED_RULES: tuple[tuple[str, str, str], ...] = (
    (r"high[- ]yield (index )?option[- ]adjusted spread", "BAMLH0A0HYM2", "ICE BofA US High Yield Index Option-Adjusted Spread"),
    (r"\b(10[- ]?y(ea)?r?|ten[- ]year)\b.{0,30}\b(treasury|yield|UST)\b|\bUST 10Y\b", "DGS10", "10-Year Treasury Constant Maturity Rate"),
    (r"\b(2[- ]?y(ea)?r?|two[- ]year)\b.{0,30}\b(treasury|yield|UST)\b|\bUST 2Y\b", "DGS2", "2-Year Treasury Constant Maturity Rate"),
    (r"\b(30[- ]?y(ea)?r?|thirty[- ]year)\b.{0,30}\btreasury\b|\bUST 30Y\b", "DGS30", "30-Year Treasury Constant Maturity Rate"),
    (r"\b30[- ]year fixed(-rate)? mortgage\b", "MORTGAGE30US", "30-Year Fixed Rate Mortgage Average"),
    (r"\bVIX\b", "VIXCLS", "CBOE Volatility Index: VIX (daily close)"),
    (r"\blabor force participation rate\b", "CIVPART", "Labor Force Participation Rate"),
    (r"\bunemployment rate\b.{0,40}\b(U\.?S\.?|United States|American)\b|\b(U\.?S\.?|United States)\b.{0,40}\bunemployment rate\b", "UNRATE", "Unemployment Rate"),
    (r"\b(effective )?federal funds (effective )?rate\b", "DFF", "Federal Funds Effective Rate"),
    (r"\binitial (jobless|unemployment) claims\b", "ICSA", "Initial Claims"),
    (r"\bnonfarm payrolls?\b", "PAYEMS", "All Employees, Total Nonfarm"),
    (r"\bS&P 500\b.{0,40}\b(close|closing|value|level|index)\b", "SP500", "S&P 500"),
    (r"\bNasdaq Composite\b", "NASDAQCOM", "NASDAQ Composite Index"),
    (r"\bDow Jones Industrial Average\b", "DJIA", "Dow Jones Industrial Average"),
    (r"\b(WTI|West Texas Intermediate)\b", "DCOILWTICO", "Crude Oil Prices: WTI"),
    (r"\bBrent\b.{0,20}\b(crude|oil)\b", "DCOILBRENTEU", "Crude Oil Prices: Brent"),
)

# Coins: the coin's name or ticker AND a price word.
COIN_RULES: tuple[tuple[str, str], ...] = (
    (r"\b(bitcoin|BTC)\b", "bitcoin"),
    (r"\b(ethereum|ETH)\b", "ethereum"),
    (r"\b(solana|SOL)\b", "solana"),
    (r"\b(dogecoin|DOGE)\b", "dogecoin"),
    (r"\bXRP\b", "ripple"),
)
_PRICE = r"\b(price|close|closing|trade|trading|value|worth)\b"
# Questions about differences, returns or comparisons aren't a series' level.
_NOT_A_LEVEL = r"\b(exceed|outperform|returns?|difference|spread between|versus|vs\.?)\b"


def match_question(question: Any) -> Match | None:
    """The one FRED series or coin the question clearly names, or None."""
    if getattr(question, "question_type", None) not in ("numeric", "discrete"):
        return None
    text = getattr(question, "question_text", "") or ""
    if re.search(_NOT_A_LEVEL, text, re.IGNORECASE) and not re.search(r"option[- ]adjusted spread", text, re.IGNORECASE):
        return None
    for pattern, series, title in FRED_RULES:
        found = re.search(pattern, text, re.IGNORECASE if series != "VIXCLS" else 0)
        if found:
            return Match("fred", series, title, found.group(0))
    if re.search(_PRICE, text, re.IGNORECASE):
        for pattern, coin in COIN_RULES:
            found = re.search(pattern, text, re.IGNORECASE if not pattern.startswith(r"\bXRP") else 0)
            if found:
                return Match("coingecko", coin, f"{coin} price (USD)", found.group(0))
    return None


# ---------------------------------------------------------------- fetching

Get = Callable[..., Any]  # requests.get-like


def fetch_fred(series: str, today: date, get: Get = requests.get, api_key: str | None = None) -> dict:
    # Pasted keys often carry a stray space or line break.
    key = (api_key if api_key is not None else os.environ["FRED_API_KEY"]).strip()
    response = get(
        FRED_URL,
        params={
            "series_id": series,
            "api_key": key,
            "file_type": "json",
            "observation_start": (today - timedelta(days=HISTORY_DAYS)).isoformat(),
        },
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    history = [
        [o["date"], float(o["value"])]
        for o in response.json().get("observations", [])
        if o.get("value") not in (None, ".", "")
    ]
    if not history:
        raise ValueError(f"FRED {series}: no observations")
    return {"latest": {"date": history[-1][0], "value": history[-1][1]}, "history": history}


def fetch_coingecko(coin: str, get: Get = requests.get) -> dict:
    response = get(
        COINGECKO_URL.format(coin=coin),
        params={"vs_currency": "usd", "days": COIN_HISTORY_DAYS, "interval": "daily"},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    prices = response.json().get("prices", [])
    history = [[date.fromtimestamp(ms / 1000).isoformat(), float(p)] for ms, p in prices]
    if not history:
        raise ValueError(f"CoinGecko {coin}: no prices")
    return {"latest": {"date": history[-1][0], "value": history[-1][1]}, "history": history}


def hard_data_for(question: Any, today: date | None = None, get: Get = requests.get) -> dict | None:
    """Match and fetch. None if nothing matches; a failure is saved, never raised."""
    match = match_question(question)
    if match is None:
        return None
    today = today or date.today()
    found = {"source": match.source, "series": match.series, "title": match.title, "rule": match.rule}
    try:
        data = fetch_fred(match.series, today, get) if match.source == "fred" else fetch_coingecko(match.series, get)
        found.update(data)
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        found["error"] = f"{type(e).__name__}{f' (HTTP {status})' if status else ''}"
    return found


# ---------------------------------------------------------------- dossier line (Build 2c)

# Words that name this series' own source: FRED, the series id, or the
# official publisher. Naming one of them beats naming another data site.
_OFFICIAL_PUBLISHER = {
    "BAMLH0A0HYM2": r"ICE BofA|ICE Data",
    "DGS10": r"treasury\.gov|Department of the Treasury|\bH\.15\b",
    "DGS2": r"treasury\.gov|Department of the Treasury|\bH\.15\b",
    "DGS30": r"treasury\.gov|Department of the Treasury|\bH\.15\b",
    "MORTGAGE30US": r"Freddie Mac|\bPMMS\b|Primary Mortgage Market Survey",
    "VIXCLS": r"\bCboe\b",
    "CIVPART": r"Bureau of Labor Statistics|\bBLS\b",
    "UNRATE": r"Bureau of Labor Statistics|\bBLS\b",
    "PAYEMS": r"Bureau of Labor Statistics|\bBLS\b",
    "DFF": r"New York Fed|Federal Reserve Bank of New York|\bEFFR\b",
    "ICSA": r"Department of Labor|\bDOL\b",
    "SP500": r"S&P Dow Jones Indices",
    "DJIA": r"S&P Dow Jones Indices",
    "DCOILWTICO": r"Energy Information Administration|\bEIA\b",
    "DCOILBRENTEU": r"Energy Information Administration|\bEIA\b",
    "bitcoin": r"CoinGecko",
    "ethereum": r"CoinGecko",
    "solana": r"CoinGecko",
    "dogecoin": r"CoinGecko",
    "ripple": r"CoinGecko",
}
# Other data sites a question may resolve on.
_OTHER_SOURCES = (
    r"\b(Yahoo Finance|Google Finance|Bloomberg|Reuters|CNBC|MarketWatch|Investing\.com|"
    r"Trading ?Economics|Wall Street Journal|WSJ|CoinMarketCap|Coinbase|Binance|Kraken|Bitstamp)\b"
)
# A different statistic than the series' one value per day/week/month.
_INTRADAY = r"\bintra-?day\b"
_STATISTICS = (
    (r"\b(maximum|highest|peak|all[- ]time high|high)\b", "a maximum"),
    (r"\b(minimum|lowest|low)\b", "a minimum"),
    (r"\b(average|mean|median)\b", "an average"),
)
# One line, a few dozen words: the dossier is cut to make room for it.
MAX_LINE_WORDS = 80


@dataclass(frozen=True)
class OfficialLine:
    text: str
    latest: float
    exact: bool  # True: the latest value may feed the unit check
    warning: str | None  # why it's only a stand-in


def _question_range(question: Any) -> tuple[float | None, float | None]:
    lower = getattr(question, "nominal_lower_bound", None)
    upper = getattr(question, "nominal_upper_bound", None)
    return (
        lower if lower is not None else getattr(question, "lower_bound", None),
        upper if upper is not None else getattr(question, "upper_bound", None),
    )


def stand_in_reason(question: Any, found: dict) -> str | None:
    """None when the series is exactly what the question asks about; else why
    it's only a stand-in. Rule-based, no model call."""
    asked = " ".join(filter(None, (getattr(question, "question_text", ""), getattr(question, "resolution_criteria", ""))))
    everything = " ".join(filter(None, (asked, getattr(question, "fine_print", ""))))
    asked_stat = re.sub(r"high[- ]yield", "", asked, flags=re.IGNORECASE)
    title = found.get("title", found.get("series", "this series"))
    what = next((label for pattern, label in _STATISTICS if re.search(pattern, asked_stat, re.IGNORECASE)), None)
    intraday = re.search(_INTRADAY, asked_stat, re.IGNORECASE)
    if intraday or what:
        what = f"an intraday {what.removeprefix('a ').removeprefix('an ')}" if intraday and what else (what or "an intraday value")
        return f"stand-in: {title}; the question asks about {what}, not this series' value"
    publisher = _OFFICIAL_PUBLISHER.get(found["series"], "(?!)")
    if found.get("source") == "fred":
        # FRED ids (e.g. DGS10) only as written; a coin id is just the coin's name.
        named_ours = re.search(rf"\bFRED\b|\b{re.escape(found['series'])}\b", everything) or re.search(
            publisher, everything, re.IGNORECASE
        )
    else:
        named_ours = re.search(publisher, everything, re.IGNORECASE)
    other = re.search(_OTHER_SOURCES, everything, re.IGNORECASE)
    source = "FRED" if found.get("source") == "fred" else "CoinGecko"
    if other and not named_ours:
        return f"stand-in: the question resolves on {other.group(0)}, not {source}"
    if found.get("source") == "coingecko" and not named_ours:
        return "stand-in: CoinGecko's daily price; the question does not name CoinGecko as its source"
    lower, upper = _question_range(question)
    value = found["latest"]["value"]
    if (lower is not None and value < lower) or (upper is not None and value > upper):
        return "stand-in: the latest value is outside the question's range, so the units or definition may differ"
    return None


def _fmt(value: float) -> str:
    return f"{value:,.2f}" if abs(value) >= 1000 else f"{value:.4g}"


def official_line(question: Any, found: dict | None) -> OfficialLine | None:
    """The one dossier line for a fetched match, or None (no match, or the
    fetch failed)."""
    if not found or "latest" not in found or not found.get("history"):
        return None
    latest_value = float(found["latest"]["value"])
    latest_date = date.fromisoformat(found["latest"]["date"])
    points = [(date.fromisoformat(d), float(v)) for d, v in found["history"]]
    year = [v for d, v in points if d >= latest_date - timedelta(days=365)] or [latest_value]
    month_ago = [(d, v) for d, v in points if d <= latest_date - timedelta(days=30)]
    source = "FRED" if found.get("source") == "fred" else "CoinGecko"
    parts = [
        f"OFFICIAL DATA ({source} {found['series']}, {found.get('title', '')}): "
        f"latest {_fmt(latest_value)} on {latest_date.isoformat()}",
        f"past-year min {_fmt(min(year))}, max {_fmt(max(year))}",
    ]
    if month_ago:
        then_date, then_value = month_ago[-1]
        change = latest_value - then_value
        parts.append(
            f"30-day change {'+' if change > 0 else ''}{_fmt(change)} (from {_fmt(then_value)} on {then_date.isoformat()})"
        )
    warning = stand_in_reason(question, found)
    text = "; ".join(parts) + "."
    text += f" WARNING: {warning}." if warning else " This series is what the question asks about."
    return OfficialLine(text=text, latest=latest_value, exact=warning is None, warning=warning)


def check_apis() -> None:
    """Credit check workflow: can we read one FRED series and one coin?
    Prints only the source, the series, the latest date and value (public data)."""
    for question_text, kind in (
        ("What will the 10-year Treasury yield be?", "numeric"),
        ("What will the price of Bitcoin be?", "numeric"),
    ):
        question = type("Q", (), {"question_type": kind, "question_text": question_text})()
        found = hard_data_for(question) or {}
        latest = found.get("latest") or {}
        print(
            f"| {found.get('source')} | {found.get('series')} | "
            f"{latest.get('date', found.get('error', '?'))} | {latest.get('value', '')} | "
            f"{len(found.get('history', []))} points |"
        )


if __name__ == "__main__":
    check_apis()
