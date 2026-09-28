"""
Build 2b: a zero-call statistical baseline for numeric questions that matched
official data (hard_data.py). SHADOW only: saved in the question log
("shadow" -> "random-walk") and scored by the scoreboard, never submitted.

Random walk from the latest value with no drift: the spread grows with the
square root of the time to the resolve date, using the series' own
historical volatility (changes between observations; log changes for prices,
plain changes for rates and percentages). Its 9 percentiles go through the
same PCHIP and 5% uniform mix as a model's forecast, and it's dropped if the
result breaks the platform rules.
"""
from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timezone
from typing import Any

from forecasting_tools import Percentile

from distributions import STEP8_PERCENTILES, mix_with_uniform, pchip_distribution
from forecast_safety import distribution_problems

# Prices move in proportion to their level: log changes. Everything else
# (yields, spreads, rates, percentages) in plain changes.
PRICE_SERIES = {"SP500", "NASDAQCOM", "DJIA", "DCOILWTICO", "DCOILBRENTEU", "VIXCLS"}
MIN_HISTORY = 20


def _resolve_date(question: Any) -> date | None:
    when = getattr(question, "scheduled_resolution_time", None) or getattr(question, "close_time", None)
    if when is None:
        return None
    if isinstance(when, datetime):
        return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).date()
    return when


def random_walk_percentiles(hard_data: dict, resolve: date) -> tuple[list[Percentile], dict] | None:
    """The 9 percentiles of the random walk at `resolve`, and what went into it."""
    history = hard_data.get("history") or []
    if len(history) < MIN_HISTORY:
        return None
    dates = [date.fromisoformat(d) for d, _ in history]
    values = [float(v) for _, v in history]
    use_log = (hard_data.get("source") == "coingecko" or hard_data.get("series") in PRICE_SERIES) and min(values) > 0
    points = [math.log(v) for v in values] if use_log else values
    changes = [b - a for a, b in zip(points, points[1:])]
    spacing_days = max(1.0, (dates[-1] - dates[0]).days / max(1, len(changes)))
    sigma_per_step = statistics.pstdev(changes)
    horizon_days = max(1, (resolve - dates[-1]).days)
    sigma = sigma_per_step * math.sqrt(horizon_days / spacing_days)
    if sigma <= 0:
        return None
    normal = statistics.NormalDist(points[-1], sigma)
    percentiles = []
    for height in STEP8_PERCENTILES:
        x = normal.inv_cdf(height)
        percentiles.append(Percentile(percentile=height, value=math.exp(x) if use_log else x))
    detail = {
        "latest": values[-1],
        "latest_date": dates[-1].isoformat(),
        "horizon_days": horizon_days,
        "log_changes": use_log,
        "sigma": round(sigma, 6),
    }
    return percentiles, detail


def random_walk_baseline(question: Any, hard_data: dict, today: date | None = None) -> tuple[Any, dict] | None:
    """The baseline as a platform-valid distribution, or None (not enough
    data, no resolve date, or it breaks the platform rules)."""
    resolve = _resolve_date(question)
    if resolve is None or "history" not in hard_data:
        return None
    built = random_walk_percentiles(hard_data, resolve)
    if built is None:
        return None
    percentiles, detail = built
    try:
        distribution = mix_with_uniform(pchip_distribution(percentiles, question), question)
    except Exception:
        return None
    if distribution_problems(distribution, question):
        return None
    return distribution, detail
