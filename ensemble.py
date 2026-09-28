"""
PLAN.md Step 6: smart ensemble, built "credits-ready".

- Binary: round 1, then round 2 ONLY if round 1 disagrees (spread over 15
  points) or the median is extreme (below 10% or above 90%). Final answer:
  the median of all forecasts (then the Step 4 adjustments).
- Numeric and multiple choice: every forecaster at once (no rounds).
- Spending tiers (credits lineup): the target spend per question is
  (remaining credit - 15% reserve) / expected remaining questions; that picks
  Full / Standard / Lean. MiniBench always runs one tier below the seasonal
  tournament. A tier change opens a GitHub issue (GitHub emails Tony).

The 'credits' lineup (PLAN.md section 3) is switched off until the architect
says so; 'gemini-free' (bot_config.GeminiPool) uses the same round-2 rule.
"""
from __future__ import annotations

import json
import os
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timezone

import requests

ROUND2_SPREAD = 0.15
ROUND2_LOW, ROUND2_HIGH = 0.10, 0.90


def round2_needed(round1: list[float]) -> bool:
    """Round 2 only when round 1 disagrees (>15 points) or the median is extreme."""
    values = [v for v in round1 if isinstance(v, (int, float))]
    if not values:
        return False
    median = statistics.median(values)
    spread = max(values) - min(values)
    # The small tolerance keeps exactly 15 points (0.55 - 0.40 in floating
    # point is 0.15000000000000002) from counting as "over 15".
    return spread > ROUND2_SPREAD + 1e-9 or median < ROUND2_LOW or median > ROUND2_HIGH


# ---------------------------------------------------------------- credits lineup (PLAN.md section 3)

OPUS_55 = "openrouter/anthropic/claude-opus-5.5"
OPUS_5 = "openrouter/anthropic/claude-opus-5"
FABLE_51 = "openrouter/anthropic/claude-fable-5.1"
GPT_SOL = "openrouter/openai/gpt-5.6-sol"
GPT_55 = "openrouter/openai/gpt-5.5"
FLASH_36 = "openrouter/google/gemini-3.6-flash"
GEMINI_31_PRO = "openrouter/google/gemini-3.1-pro-preview"
# Not in any tier or backup chain (architect, 29 Sep); only its price is
# still checked by the pre-flight, for reference.
PRICE_CHECK_ONLY = (FABLE_51,)

# When a model errors, refuses or times out, the next one is tried.
BACKUPS: dict[str, list[str]] = {
    OPUS_55: [OPUS_5],
    GPT_SOL: [GPT_55],
    FLASH_36: [GEMINI_31_PRO],
}


@dataclass(frozen=True)
class Tier:
    name: str
    binary_round1: tuple[str, ...]
    binary_round2: tuple[str, ...]
    rough_cost: float  # dollars per question: the tier's most expensive column of COST_TABLE

    @property
    def all_forecasters(self) -> tuple[str, ...]:
        """Numeric and multiple choice: everyone at once."""
        return self.binary_round1 + self.binary_round2


# OpenRouter prices, dollars per million tokens (prompt, completion), from the
# Credits pre-flight run 36472633695 (28 Sep 2026). Used by the spend guards
# (spend.py) to reserve an estimate per paid call. Rerun the pre-flight to refresh.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "anthropic/claude-opus-5.5": (4.00, 20.00),
    "openai/gpt-5.6-sol": (2.00, 10.00),
    "google/gemini-3.6-flash": (0.75, 3.75),
    "anthropic/claude-fable-5.1": (10.00, 50.00),
    "anthropic/claude-opus-5": (5.00, 25.00),
    "openai/gpt-5.5": (5.00, 30.00),
    "google/gemini-3.1-pro-preview": (2.00, 12.00),
}
# A model missing from the table is estimated at the most expensive price.
UNKNOWN_MODEL_PRICE = (10.00, 50.00)
MODEL_PRICES = {f"openrouter/{k}": v for k, v in MODEL_PRICES.items()}

# Credits 4b (cost_table.py, Credits pre-flight runs 36472957797 and, after
# Fable 5.1 was removed, 36481725679, 28 Sep 2026):
# dollars per question = real OpenRouter prices x prompt tokens measured from
# our question logs (binary 7,143; numeric 7,476; discrete 4,991; multiple
# choice 6,732; only 1-2 logged questions per type so far) + an ASSUMED 8,000
# output tokens per forecast (high reasoning). Research and parsing are free
# (Flash-Lite pool). Every Flash 3.6 slot counted as paid (it tries the free
# AI Studio key first, 4c), so these are upper-side numbers.
COST_TABLE: dict[str, dict[str, float]] = {
    "full": {"binary": 0.32, "binary+r2": 0.64, "numeric": 0.64, "discrete": 0.61, "multiple_choice": 0.63},
    "standard": {"binary": 0.32, "binary+r2": 0.39, "numeric": 0.39, "discrete": 0.37, "multiple_choice": 0.39},
    "lean": {"binary": 0.22, "binary+r2": 0.26, "numeric": 0.26, "discrete": 0.25, "multiple_choice": 0.26},
}

TIERS: dict[str, Tier] = {
    # Round 2 adds one more forecast per family (architect, 29 Sep: Fable 5.1
    # removed, 16.1 vs Opus 5's 15.8 on the leaderboard at 2.5x the price).
    "full": Tier("full", (OPUS_55, GPT_SOL, FLASH_36), (OPUS_55, GPT_SOL, FLASH_36), max(COST_TABLE["full"].values())),
    "standard": Tier("standard", (OPUS_55, GPT_SOL, FLASH_36), (FLASH_36, FLASH_36), max(COST_TABLE["standard"].values())),
    "lean": Tier("lean", (OPUS_55, FLASH_36), (FLASH_36,), max(COST_TABLE["lean"].values())),
}
TIER_ORDER = ("full", "standard", "lean")
CREDIT_RESERVE = 0.15
SEASON_END = date(2027, 1, 6)
# Rough questions per day: seasonal plus MiniBench (retune once we see real numbers).
QUESTIONS_PER_DAY = 12


def target_spend_per_question(remaining: float, limit: float, expected_questions: float) -> float:
    """(remaining credit - 15% reserve) / expected remaining questions."""
    if expected_questions <= 0:
        return 0.0
    return max(0.0, remaining - CREDIT_RESERVE * limit) / expected_questions


def choose_tier(target: float) -> str:
    """The richest tier whose cost (its most expensive question type) fits the
    target spend per question."""
    for name in TIER_ORDER:
        if target >= TIERS[name].rough_cost:
            return name
    return "lean"


def tier_below(name: str) -> str:
    """MiniBench always runs one tier below the seasonal tournament."""
    index = TIER_ORDER.index(name)
    return TIER_ORDER[min(index + 1, len(TIER_ORDER) - 1)]


def expected_remaining_questions(today: date | None = None) -> float:
    today = today or datetime.now(timezone.utc).date()
    return max(1, (SEASON_END - today).days) * QUESTIONS_PER_DAY


def openrouter_credit() -> tuple[float, float] | None:
    """(remaining, limit) in dollars for OPENROUTER_API_KEY, or None if unknown."""
    try:
        response = requests.get(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()["data"]
        if data.get("limit") is None or data.get("limit_remaining") is None:
            return None
        return float(data["limit_remaining"]), float(data["limit"])
    except Exception:
        return None


def notify_tier_change(old: str | None, new: str, target: float) -> bool:
    """Open a GitHub issue (GitHub emails the repo owner). Never raises."""
    if old == new:
        return False
    try:
        response = requests.post(
            f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/issues",
            headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"},
            json={
                "title": f"Spending tier changed: {old or 'none'} -> {new}",
                "body": f"Target spend per question is now about ${target:.2f}. "
                "MiniBench runs one tier below. (Automatic message from the bot.)",
            },
            timeout=30,
        )
        response.raise_for_status()
        return True
    except Exception:
        return False


def open_alert_issue(title: str, body: str) -> bool:
    """Credits 4d: open a GitHub issue (GitHub emails Tony), unless an open
    issue already has this title (alert titles carry the date: once a day).
    Never raises."""
    try:
        repo = os.environ["GITHUB_REPOSITORY"]
        headers = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}
        existing = requests.get(
            f"https://api.github.com/repos/{repo}/issues", headers=headers,
            params={"state": "open", "per_page": 100}, timeout=30,
        )
        existing.raise_for_status()
        if any(issue.get("title") == title for issue in existing.json()):
            return False
        response = requests.post(
            f"https://api.github.com/repos/{repo}/issues", headers=headers,
            json={"title": title, "body": body}, timeout=30,
        )
        response.raise_for_status()
        return True
    except Exception:
        return False


def openrouter_key_usage() -> float | None:
    """Total dollars this OPENROUTER_API_KEY has spent (OpenRouter's own count), or None."""
    try:
        response = requests.get(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
            timeout=30,
        )
        response.raise_for_status()
        return float(response.json()["data"]["usage"])
    except Exception:
        return None


def tier_record(tier: str, target: float) -> str:
    return json.dumps({"tier": tier, "target": round(target, 4), "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
