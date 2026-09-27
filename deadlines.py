"""
Time limits so no question is missed (PLAN.md Step 3, "Never miss").

- The full set of forecasts must be done 15 minutes before a question closes.
  Forecasts still running then are dropped and the finished ones are combined
  (median) and submitted.
- If none finished, one quick forecast is made, which must end 2 minutes
  before the close so there is time to submit it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

FULL_SET_CUTOFF = timedelta(minutes=15)
SUBMIT_MARGIN = timedelta(minutes=2)
# Below this, a limit is too short to be worth waiting for the cut-off.
MIN_FORECAST_SECONDS = 60
# Below this, there's no time left even for a quick forecast.
MIN_QUICK_FORECAST_SECONDS = 20


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def planned_forecast_timeout(
    close_time: datetime | None, now: datetime | None = None
) -> float | None:
    """Seconds a planned forecast may take. None = no limit (no close time)."""
    if close_time is None:
        return None
    now = now or datetime.now(timezone.utc)
    close = _as_utc(close_time)
    until_cutoff = (close - FULL_SET_CUTOFF - now).total_seconds()
    if until_cutoff >= MIN_FORECAST_SECONDS:
        return until_cutoff
    # Already inside the last 15 minutes: use all the time there is.
    return max(0.0, (close - SUBMIT_MARGIN - now).total_seconds())


def quick_forecast_timeout(
    close_time: datetime | None, now: datetime | None = None
) -> float | None:
    """
    Seconds the quick forecast may take (None = no limit), or 0 if there's no
    time left to make one.
    """
    if close_time is None:
        return None
    now = now or datetime.now(timezone.utc)
    seconds = (_as_utc(close_time) - SUBMIT_MARGIN - now).total_seconds()
    return seconds if seconds >= MIN_QUICK_FORECAST_SECONDS else 0.0
