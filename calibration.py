"""
Calibration report (read-only, manual): how well calibrated our LIVE
forecasts were on every resolved question so far, seasonal and MiniBench
apart, per question type. Numbers only.

Data: the same as the Scoreboard (scoreboard.py): the latest submitted
tournament question log per question (fall26-data), resolutions from the
Metaculus API, and the Scoreboard's log-score formulas.

- Binary: share resolved Yes vs our mean forecast; live log score vs a
  log-odds stretch k in STRETCH_GRID, i.e. logit(p') = k * logit(p)
  (forecast_safety.stretch, as in the "stretch-1.5" shadow), then the live
  2-98% clip (forecast_safety.clip_binary), applied to the submitted p.
  Leave-one-out: for each question the best k on all the others is applied
  to it (ties: the k nearer 1.0); the LOO total/mean and how often each k
  was picked.
- Numeric / discrete: where the outcome fell on our submitted CDF (its
  declared percentiles, linear in between): inside / below / above our
  10-90% and 25-75% ranges. An outcome outside the question's range sits at
  the CDF's edge value (the Scoreboard uses the same tail).
- Multiple choice: our probability on the option that happened, log score.
- MiniBench binary patterns: Yes-rate per wording template, and per
  "threshold above / below the current value" where both numbers can be read
  from the question text, background or our research dossier.

The report holds forecast aggregates, so it is saved ONLY to fall26-data
(reports/calibration_<UTC date>.md); the job log gets a one-line status.
`build_report(records, resolution_of)` works on in-memory records (tests).
"""
from __future__ import annotations

import logging
import math
import re
import statistics
from datetime import datetime, timezone
from typing import Any, Callable

from bot_helpers import PUBLIC_LOGGER_NAME
from forecast_safety import clip_binary, stretch
from scoreboard import binary_score, forecast_score

logger = logging.getLogger(PUBLIC_LOGGER_NAME)

STRETCH_GRID = (1.0, 1.2, 1.5, 1.73, 2.0)
RANGES = ((0.10, 0.90), (0.25, 0.75))
GROUPS = ("seasonal", "MiniBench", "unlabelled")


# ---------------------------------------------------------------- binary (pure)


def stretched(p: float, k: float) -> float:
    """The submitted p stretched by k in log-odds, then the live 2-98% clip."""
    return clip_binary(stretch(p, k))


def stretch_total(rows: list[tuple[float, bool]], k: float) -> float:
    return sum(binary_score(stretched(p, k), yes) for p, yes in rows)


def best_k(rows: list[tuple[float, bool]], grid: tuple[float, ...] = STRETCH_GRID) -> float:
    """The k with the best total log score (ties: nearer 1.0, then smaller)."""
    return max(grid, key=lambda k: (round(stretch_total(rows, k), 12), -abs(k - 1.0), -k))


def leave_one_out(rows: list[tuple[float, bool]], grid: tuple[float, ...] = STRETCH_GRID) -> tuple[list[float], dict[float, int]]:
    """Each question's score with the best k on all the others, and how often each k was picked."""
    scores, picks = [], {k: 0 for k in grid}
    for i, (p, yes) in enumerate(rows):
        k = best_k(rows[:i] + rows[i + 1:], grid)
        picks[k] += 1
        scores.append(binary_score(stretched(p, k), yes))
    return scores, picks


def binary_section(rows: list[tuple[float, bool]]) -> list[str]:
    n = len(rows)
    lines = [
        f"Binary: {n} resolved; resolved Yes {sum(y for _, y in rows) / n:.1%}; "
        f"our mean forecast {statistics.fmean(p for p, _ in rows):.1%}.",
        "",
        "| Stretch k | Total log score | Mean log score |",
        "|---|---|---|",
    ]
    for k in STRETCH_GRID:
        total = stretch_total(rows, k)
        lines.append(f"| {k:g}{' (live)' if k == 1.0 else ''} | {total:.4f} | {total / n:.4f} |")
    if n >= 2:
        scores, picks = leave_one_out(rows)
        lines += [
            "",
            f"Leave-one-out (best k on the other {n - 1}): total {sum(scores):.4f}, "
            f"mean {statistics.fmean(scores):.4f} (live: {stretch_total(rows, 1.0) / n:.4f}).",
            "Picked: " + ", ".join(f"k={k:g} {count}x" for k, count in picks.items()) + ".",
        ]
    return lines


# ---------------------------------------------------------------- numeric (pure)


def cdf_at_outcome(forecast: dict, resolution: str) -> float | None:
    """Our CDF at the outcome (declared percentiles, linear in between). Out of
    the question's range: the CDF's edge value (the Scoreboard's tail)."""
    points = (forecast.get("declared_percentiles") or []) if isinstance(forecast, dict) else []
    if len(points) < 2:
        return None
    values = [float(p["value"]) for p in points]
    cdf = [float(p["percentile"]) for p in points]
    if resolution == "below_lower_bound":
        return cdf[0]
    if resolution == "above_upper_bound":
        return cdf[-1]
    try:
        answer = float(resolution)
    except (TypeError, ValueError):
        return None
    if answer <= values[0]:
        return cdf[0]
    if answer >= values[-1]:
        return cdf[-1]
    for i in range(1, len(values)):
        if answer <= values[i]:
            width = values[i] - values[i - 1]
            share = (answer - values[i - 1]) / width if width > 0 else 1.0
            return cdf[i - 1] + share * (cdf[i] - cdf[i - 1])
    return None


def where(level: float, low: float, high: float) -> str:
    """'below' / 'inside' / 'above' our low-high range, from the CDF at the outcome."""
    return "below" if level < low else "above" if level > high else "inside"


def numeric_section(kind: str, items: list[tuple[float, float]]) -> list[str]:
    """items: (CDF at the outcome, log score)."""
    n = len(items)
    lines = [
        f"{kind}: {n} resolved; mean live log score {statistics.fmean(s for _, s in items):.4f}.",
        "",
        "| Our range | Below | Inside | Above | Inside if calibrated |",
        "|---|---|---|---|---|",
    ]
    for low, high in RANGES:
        counts = {"below": 0, "inside": 0, "above": 0}
        for level, _ in items:
            counts[where(level, low, high)] += 1
        cells = " | ".join(f"{counts[c]} ({counts[c] / n:.0%})" for c in ("below", "inside", "above"))
        lines.append(f"| {low:.0%}-{high:.0%} | {cells} | {high - low:.0%} |")
    return lines


# ---------------------------------------------------------------- multiple choice (pure)


def option_probability(forecast: Any, option: str) -> float | None:
    if not isinstance(forecast, dict):
        return None
    for o in forecast.get("predicted_options", []):
        if o.get("option_name") == option:
            return float(o["probability"])
    return None


def multiple_choice_section(items: list[tuple[Any, int, float, float]]) -> list[str]:
    """items: (post, number of options, our p on the option that happened, log score)."""
    lines = [
        f"Multiple choice: {len(items)} resolved; mean p on the option that happened "
        f"{statistics.fmean(p for _, _, p, _ in items):.1%}; mean log score "
        f"{statistics.fmean(s for *_, s in items):.4f}.",
        "",
        "| Post | Options | Our p on the outcome | Log score |",
        "|---|---|---|---|",
    ]
    lines += [f"| {post} | {count} | {p:.1%} | {score:.4f} |" for post, count, p, score in items]
    return lines


# ---------------------------------------------------------------- MiniBench patterns (pure)

TEMPLATES = (
    ("between", r"\bbetween\b"),
    ("below / fall", r"\b(below|under|lower than|less than|fewer than|fall|falls|drop|drops|decline|declines|decrease|decreases)\b"),
    ("exceed / above", r"\b(exceed|exceeds|above|over|higher than|greater than|more than|at least|rise|rises|increase|increases|surpass|surpasses|reach|reaches)\b"),
    ("before <date>", r"\bbefore\b"),
)


def template_of(text: str) -> str:
    """The question's wording template (first match in TEMPLATES order), or 'other'."""
    text = re.sub(r"\bfall (19|20)\d\d\b", "", text or "", flags=re.IGNORECASE)  # "Fall 2026" is a season
    for name, pattern in TEMPLATES:
        if re.search(pattern, text, re.IGNORECASE):
            return name
    return "other"


_MONTHS = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_DATES = (
    rf"\b{_MONTHS}\s+\d{{1,2}}(st|nd|rd|th)?,?\s+(19|20)\d\d\b",
    rf"\b\d{{1,2}}(st|nd|rd|th)?\s+{_MONTHS},?\s+(19|20)\d\d\b",
    rf"\b{_MONTHS}\s+\d{{1,2}}(st|nd|rd|th)?\b",
    rf"\b{_MONTHS}\s+(19|20)\d\d\b",
    r"\b(19|20)\d\d-\d\d-\d\d\b",
    r"\b\d{1,2}/\d{1,2}/(19|20)?\d\d\b",
    r"\b(Q[1-4]|H[12]|FY)\s*(19|20)\d\d\b",
    r"\b(in|of|by|since|from|to|for|during|end of)\s+(19|20)\d\d\b",
    r"\b(19|20)\d\d\b(?![\d,.])",
)
_NUMBER = r"-?\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*(%|percent|k\b|thousand|million|billion|trillion|bn\b|m\b)?"
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "bn": 1e9, "billion": 1e9, "trillion": 1e12}
_THRESHOLD_CUE = (
    r"\b(between|below|under|lower than|less than|fewer than|exceed|exceeds|above|over|higher than|"
    r"greater than|more than|at least|at most|reach|reaches|surpass|surpasses|to|of)\b\s*"
)
_CURRENT_CUE = (
    r"\b(currently|current(ly)? (value|level|price|rate|reading)|latest|most recent|as of|stood at|stands at|"
    r"closed at|was at|is at|is now|now at|trading at|last (value|close|reading|reported))\b"
)
_NUMBER_RE = re.compile(_NUMBER, re.IGNORECASE)
_THRESHOLD_RE = re.compile(_THRESHOLD_CUE + f"(?P<number>{_NUMBER})", re.IGNORECASE)
_CURRENT_RE = re.compile(_CURRENT_CUE + r"[^\d$\-.]{0,60}" + f"(?P<number>{_NUMBER})", re.IGNORECASE)


def _without_dates(text: str) -> str:
    for pattern in _DATES:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    return text


def _number(text: str) -> float:
    found = _NUMBER_RE.search(text)
    value = float(found.group(1).replace(",", "") + (found.group(2) or ""))
    if found.group(0).startswith("-"):
        value = -value
    return value * _SCALE.get((found.group(3) or "").lower(), 1.0)


def threshold_of(question_text: str) -> float | None:
    """The first number right after a threshold word in the question title."""
    found = _THRESHOLD_RE.search(_without_dates(question_text or ""))
    return _number(found.group("number")) if found else None


def current_value_of(*texts: str) -> float | None:
    """The first number within a few words after a 'current value' cue, in the texts in order."""
    for text in texts:
        found = _CURRENT_RE.search(_without_dates(text or ""))
        if found:
            return _number(found.group("number"))
    return None


def threshold_side(record: dict) -> str | None:
    """'threshold above current' / 'threshold below current', or None if not
    readable (no threshold, no current value, or units too far apart)."""
    question = record.get("question") or {}
    threshold = threshold_of(question.get("question_text", ""))
    hard = record.get("hard_data") or {}
    current = (hard.get("latest") or {}).get("value") if isinstance(hard, dict) else None
    if current is None:
        current = current_value_of(
            question.get("question_text", ""),
            question.get("background_info", "") or "",
            question.get("resolution_criteria", "") or "",
            (record.get("research") or {}).get("text", "") or "",
        )
    if threshold is None or current is None or threshold == current:
        return None
    if threshold * current <= 0 or max(abs(threshold), abs(current)) > 10 * min(abs(threshold), abs(current)):
        return None  # a sign change or more than 10x apart: probably not the same quantity
    return "threshold above current" if threshold > current else "threshold below current"


def yes_rate_table(label: str, groups: dict[str, list[bool]]) -> list[str]:
    lines = [f"| {label} | Questions | Resolved Yes |", "|---|---|---|"]
    for name, outcomes in groups.items():
        if outcomes:
            lines.append(f"| {name} | {len(outcomes)} | {sum(outcomes)} ({sum(outcomes) / len(outcomes):.0%}) |")
    return lines


def minibench_patterns(binary: list[tuple[dict, bool]]) -> list[str]:
    templates: dict[str, list[bool]] = {name: [] for name, _ in TEMPLATES}
    templates["other"] = []
    sides: dict[str, list[bool]] = {"threshold above current": [], "threshold below current": []}
    for record, yes in binary:
        templates[template_of((record.get("question") or {}).get("question_text", ""))].append(yes)
        side = threshold_side(record)
        if side:
            sides[side].append(yes)
    classified = sum(len(v) for v in sides.values())
    lines = ["**MiniBench binary patterns:**", "", *yes_rate_table("Wording template", templates), ""]
    lines.append(f"Threshold vs current value at open time: {classified} of {len(binary)} question(s) could be classified.")
    if classified:
        lines += ["", *yes_rate_table("Threshold", sides)]
    return lines


# ---------------------------------------------------------------- report (pure)


def group_of(record: dict) -> str:
    """'seasonal' / 'MiniBench' from the question log's 'seasonal' flag (or the
    tournament slugs of older logs)."""
    if "seasonal" in record:
        return "seasonal" if record["seasonal"] else "MiniBench"
    slugs = " ".join(str(s) for s in (record.get("question") or {}).get("tournament_slugs") or [])
    return "MiniBench" if "minibench" in slugs.lower() else "unlabelled"


def build_report(records: list[dict], resolution_of: Callable[[Any], str | None], stamp: str = "") -> tuple[str, int]:
    """The markdown report from the latest submitted log per question; returns (report, questions scored)."""
    by_group: dict[str, dict[str, list]] = {g: {"binary": [], "binary_records": [], "numeric": [], "discrete": [],
                                                 "multiple_choice": []} for g in GROUPS}
    scored = 0
    for record in records:
        question = record.get("question") or {}
        resolution = resolution_of(question.get("id_of_post"))
        if not resolution:
            continue
        kind, forecast = question.get("question_type"), record.get("final_forecast")
        score = forecast_score(kind, forecast, resolution)
        if score is None:
            continue  # annulled / ambiguous, or not scorable: the Scoreboard skips it too
        bucket = by_group[group_of(record)]
        if kind == "binary":
            bucket["binary"].append((float(forecast), resolution.lower() == "yes"))
            bucket["binary_records"].append((record, resolution.lower() == "yes"))
        elif kind in ("numeric", "discrete"):
            level = cdf_at_outcome(forecast, resolution)
            if level is None:
                continue
            bucket[kind].append((level, score))
        elif kind == "multiple_choice":
            p = option_probability(forecast, resolution)
            bucket[kind].append((question.get("id_of_post"), len(forecast.get("predicted_options", [])), p, score))
        scored += 1
    lines = [
        f"## Calibration report {stamp}".rstrip(),
        "",
        f"{scored} resolved question(s) scored. Live forecasts only: the latest submitted log per question "
        "(as the Scoreboard). Log scores are the Scoreboard's (natural log, higher = better). Stretch: "
        "logit(p') = k * logit(p) on the submitted p, then the live 2-98% clip.",
    ]
    for group in GROUPS:
        bucket = by_group[group]
        if not any(bucket[k] for k in ("binary", "numeric", "discrete", "multiple_choice")):
            if group != "unlabelled":
                lines += ["", f"### {group}", "", "No resolved questions yet."]
            continue
        lines += ["", f"### {group}"]
        if bucket["binary"]:
            lines += ["", *binary_section(bucket["binary"])]
        for kind in ("numeric", "discrete"):
            if bucket[kind]:
                lines += ["", *numeric_section(kind.capitalize(), bucket[kind])]
        if bucket["multiple_choice"]:
            lines += ["", *multiple_choice_section(bucket["multiple_choice"])]
        if group == "MiniBench" and bucket["binary_records"]:
            lines += ["", *minibench_patterns(bucket["binary_records"])]
    return "\n".join(lines) + "\n", scored


# ---------------------------------------------------------------- run (network)


def main() -> None:
    from bot_helpers import configure_public_logging, silence_noisy_dependencies

    silence_noisy_dependencies()
    configure_public_logging()
    import os

    import scoreboard

    token = os.environ["DATA_REPO_TOKEN"]
    records = scoreboard.latest_submitted_records(token)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report, scored = build_report(records, scoreboard.resolution_lookup(), stamp)
    path = f"reports/calibration_{stamp}.md"
    scoreboard.save_markdown(path, report, token, f"Calibration report {stamp}")
    # Public log: the path and the count only (never the report).
    logger.info(f"saved {path}, {scored} questions")


if __name__ == "__main__":
    main()
