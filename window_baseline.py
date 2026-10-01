"""
Empirical window baseline (architect, 1 Oct): replaces the random walk
(stat_baseline.py) for numeric questions matched to official data. SHADOW
only: saved in the question log ("shadow" -> "window"), scored by the
Scoreboard, never submitted.

- The series' full daily history (FRED: every observation; CoinGecko's
  public API: the last 365 days only).
- Every past window of the question's length (open date to resolve date),
  one starting at each observation. Windows starting within +/-25% of
  today's value are used if there are at least 30; otherwise all windows.
- The statistic the question asks for: the END value, the MAXIMUM or the
  MINIMUM over the window (same words as the official-data stand-in rule).
  An average, or anything else, gets no window baseline.
- Each window is taken relative to its start and put on today's value:
  ratio x today for prices and coins (log-type series), change + today for
  rates and percentages (a ratio near zero would explode).
- Its 9 percentiles go through the same PCHIP + 5% uniform mix as a model's
  forecast; dropped if the result breaks the platform rules.
- Daily closes understate an intraday maximum (and overstate an intraday
  minimum) a little: kept as a warning in the detail and the public log.
"""
from __future__ import annotations

import bisect
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import requests

from distributions import STEP8_PERCENTILES, mix_with_uniform, pchip_distribution
from forecast_safety import distribution_problems
from forecasting_tools import Percentile

SIMILAR_LEVEL = 0.25
MIN_SIMILAR_WINDOWS = 30
MIN_WINDOWS = 30  # fewer windows than this in all: no baseline
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
# Same split as the random walk: ratios for prices and coins, plain changes otherwise.
RATIO_SERIES = {"SP500", "NASDAQCOM", "DJIA", "DCOILWTICO", "DCOILBRENTEU", "VIXCLS"}
_MAX = r"\b(maximum|highest|peak|all[- ]time high|high)\b"
_MIN = r"\b(minimum|lowest|low)\b"
_AVERAGE = r"\b(average|mean|median)\b"
_INTRADAY = r"\bintra-?day\b"


def statistic_for(question: Any) -> str | None:
    """'max', 'min' or 'end'; None for an average (not supported)."""
    asked = " ".join(filter(None, (getattr(question, "question_text", ""), getattr(question, "resolution_criteria", ""))))
    asked = re.sub(r"high[- ]yield", "", asked, flags=re.IGNORECASE)
    if re.search(_AVERAGE, asked, re.IGNORECASE):
        return None
    if re.search(_MAX, asked, re.IGNORECASE):
        return "max"
    if re.search(_MIN, asked, re.IGNORECASE):
        return "min"
    return "end"


def _day(when: Any) -> date | None:
    if when is None:
        return None
    if isinstance(when, datetime):
        return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).date()
    if isinstance(when, str):
        return datetime.fromisoformat(when.replace("Z", "+00:00")).date()
    return when


def window_days(question: Any) -> int | None:
    start = _day(getattr(question, "open_time", None))
    end = _day(getattr(question, "scheduled_resolution_time", None) or getattr(question, "close_time", None))
    if start is None or end is None or end <= start:
        return None
    return (end - start).days


def window_sample(history: list, days: int, statistic: str, today_value: float, ratio: bool) -> tuple[list[float], dict]:
    """The statistic of every past window, put on today's value, and what went into it."""
    dates = [date.fromisoformat(d) if isinstance(d, str) else d for d, _ in history]
    values = [float(v) for _, v in history]
    starts = []
    for i, first in enumerate(dates):
        end = first + timedelta(days=days)
        if end > dates[-1]:
            break  # this window (and every later one) runs past the end of the history
        j = bisect.bisect_right(dates, end) - 1  # the last observation in the window (gaps are fine)
        if ratio and values[i] <= 0:
            continue
        starts.append((i, j))
    similar = [(i, j) for i, j in starts if abs(values[i] - today_value) <= SIMILAR_LEVEL * abs(today_value)]
    use_similar = len(similar) >= MIN_SIMILAR_WINDOWS
    chosen = similar if use_similar else starts
    sample = []
    for i, j in chosen:
        window = values[i : j + 1]
        stat = {"end": window[-1], "max": max(window), "min": min(window)}[statistic]
        sample.append(stat / values[i] * today_value if ratio else stat - values[i] + today_value)
    detail = {
        "statistic": statistic,
        "window_days": days,
        "windows": len(chosen),
        "similar_level": use_similar,
        "similar_windows": len(similar),
        "all_windows": len(starts),
        "history_from": dates[0].isoformat() if dates else None,
        "scaling": "ratio" if ratio else "change",
    }
    return sample, detail


def _quantile(sorted_values: list[float], p: float) -> float:
    position = p * (len(sorted_values) - 1)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (position - low) * (sorted_values[high] - sorted_values[low])


def fetch_full_history(found: dict, get: Callable[..., Any] = requests.get, api_key: str | None = None) -> list:
    """FRED: every observation (no start date). Other sources: what was
    already fetched (CoinGecko's public API gives 365 days)."""
    if found.get("source") != "fred":
        return list(found.get("history") or [])
    import os

    key = (api_key if api_key is not None else os.environ["FRED_API_KEY"]).strip()
    response = get(FRED_URL, params={"series_id": found["series"], "api_key": key, "file_type": "json"}, timeout=30)
    response.raise_for_status()
    return [[o["date"], float(o["value"])] for o in response.json().get("observations", []) if o.get("value") not in (None, ".", "")]


def window_baseline(question: Any, found: dict, history: list) -> tuple[Any | None, dict]:
    """(platform-valid distribution or None, detail). Without a baseline the
    detail says why ("skipped"): an average, no dates, too few windows, or the
    result broke the platform rules."""
    statistic = statistic_for(question)
    days = window_days(question)
    if statistic is None:
        return None, {"skipped": "the question asks for an average"}
    if days is None or "latest" not in found:
        return None, {"skipped": "no open/resolve dates or no latest value"}
    today_value = float(found["latest"]["value"])
    ratio = found.get("source") == "coingecko" or found.get("series") in RATIO_SERIES
    if ratio and today_value <= 0:
        return None, {"skipped": "latest value not positive"}
    sample, detail = window_sample(history, days, statistic, today_value, ratio)
    if len(sample) < MIN_WINDOWS:
        detail["skipped"] = f"too few windows ({len(sample)} < {MIN_WINDOWS})"
        return None, detail
    asked = " ".join(filter(None, (getattr(question, "question_text", ""), getattr(question, "resolution_criteria", ""))))
    if statistic in ("max", "min") and re.search(_INTRADAY, asked, re.IGNORECASE):
        detail["warning"] = (
            "daily closes vs an intraday " + ("maximum: the true maximum is a little higher"
                                              if statistic == "max" else "minimum: the true minimum is a little lower")
        )
    ordered = sorted(sample)
    percentiles = [Percentile(percentile=p, value=_quantile(ordered, p)) for p in STEP8_PERCENTILES]
    # PCHIP needs strictly increasing values.
    for k in range(1, len(percentiles)):
        if percentiles[k].value <= percentiles[k - 1].value:
            percentiles[k] = Percentile(percentile=percentiles[k].percentile, value=percentiles[k - 1].value + 1e-6 * max(1.0, abs(today_value)))
    detail["percentiles"] = {str(p.percentile): round(p.value, 6) for p in percentiles}
    try:
        distribution = mix_with_uniform(pchip_distribution(percentiles, question), question)
    except Exception as e:
        detail["skipped"] = f"could not build the distribution ({type(e).__name__})"
        return None, detail
    problems = distribution_problems(distribution, question)
    if problems:
        detail["skipped"] = "broke the platform rules"
        return None, detail
    return distribution, detail
