"""
Scoreboard: scores every resolved tournament question logged in fall26-data.

- Live forecast, per question type:
  - binary: ln(p) if Yes, ln(1 - p) if No
  - multiple choice: ln(probability we gave the option that happened)
  - numeric / discrete: ln(probability mass our CDF put in the grid bucket the
    answer fell in; below / above the range use the tail mass)
  Higher (closer to 0) is better. These are raw log scores, not Metaculus
  peer scores.
- Shadow variants (every type, PLAN.md Step 10, shadow.py): the same score on
  the same questions, and the mean difference from live, per type (e.g.
  numeric "window" and "window-blend", one row each).
- Per model (private: saved to fall26-data only, never on the job page):
  each forecast entry's `forecast_model` scored alone (every type), and the
  binary subset medians "claude-only", "flash-only", "no-agent" through the
  live final steps (forecast_safety.adjust_binary), each with the mean diff
  vs live; split before / since FRONTIER_SWITCH_ON.
Counts per type are shown. The report goes to fall26-data (scoreboard/) and
the job page. Weekly workflow "Scoreboard" plus a manual button.
"""
from __future__ import annotations

import base64
import json
import math
import os
import statistics
from datetime import datetime, timezone
from typing import Any

import requests

from question_log import DATA_REPO

EPS = 1e-6
TYPES = ("binary", "multiple_choice", "numeric", "discrete")


# ---------------------------------------------------------------- scores (pure)


def binary_score(p: float, yes: bool) -> float:
    p = min(max(p, EPS), 1 - EPS)
    return math.log(p) if yes else math.log(1 - p)


def multiple_choice_score(forecast: dict, winning_option: str) -> float | None:
    options = {o["option_name"]: o["probability"] for o in forecast.get("predicted_options", [])}
    if winning_option not in options:
        return None
    return math.log(max(options[winning_option], EPS))


def numeric_score(forecast: dict, resolution: str) -> float | None:
    """ln(mass in the bucket containing the answer) on the declared CDF."""
    points = forecast.get("declared_percentiles") or []
    if len(points) < 2:
        return None
    values = [p["value"] for p in points]
    cdf = [p["percentile"] for p in points]
    if resolution == "below_lower_bound":
        return math.log(max(cdf[0], EPS))
    if resolution == "above_upper_bound":
        return math.log(max(1 - cdf[-1], EPS))
    try:
        answer = float(resolution)
    except ValueError:
        return None
    if answer <= values[0]:
        return math.log(max(cdf[0], EPS))
    if answer > values[-1]:
        return math.log(max(1 - cdf[-1], EPS))
    for i in range(1, len(values)):
        if answer <= values[i]:
            return math.log(max(cdf[i] - cdf[i - 1], EPS))
    return None


def forecast_score(question_type: str | None, forecast: Any, resolution: str) -> float | None:
    """The log score of one forecast (live or shadow) in the saved JSON form."""
    if forecast is None:
        return None
    if question_type == "binary":
        if resolution.lower() not in ("yes", "no"):
            return None
        return binary_score(float(forecast), resolution.lower() == "yes")
    if question_type == "multiple_choice" and isinstance(forecast, dict):
        return multiple_choice_score(forecast, resolution)
    if question_type in ("numeric", "discrete") and isinstance(forecast, dict):
        return numeric_score(forecast, resolution)
    return None


def live_score(record: dict, resolution: str) -> float | None:
    return forecast_score(record["question"].get("question_type"), record.get("final_forecast"), resolution)


def shadow_answer_rate(records: list[dict]) -> str:
    """The shadow forecaster's answer rate per type, over every attempt
    (including runs where the live forecast failed): is it a usable backup?"""
    counts: dict[str, list[int]] = {}
    for record in records:
        shadow = record.get("shadow_model")
        if not shadow or "status" not in shadow:
            continue
        kind = (record.get("question") or {}).get("question_type", "?")
        row = counts.setdefault(kind, [0, 0, 0])
        row[0] += 1
        row[1] += shadow["status"] == "ok"
        row[2] += bool(record.get("submitted")) is False and shadow["status"] == "ok"
    if not counts:
        return "Shadow forecaster: no attempts logged yet."
    lines = ["| Type | Shadow attempts | Answered | Answered when live had no forecast |", "|---|---|---|---|"]
    for kind in (*TYPES, *sorted(k for k in counts if k not in TYPES)):
        if kind in counts:
            asked, answered, rescued = counts[kind]
            lines.append(f"| {kind} | {asked} | {answered} ({answered / asked:.0%}) | {rescued} |")
    return "\n".join(lines)


def report_markdown(scored: list[dict]) -> str:
    """scored: {"type", "live": float, "shadow": {name: float}}"""
    lines = ["| Type | Resolved questions | Mean live log score |", "|---|---|---|"]
    for t in ("all", *TYPES):
        rows = [r for r in scored if t == "all" or r["type"] == t]
        if rows:
            lines.append(f"| {t} | {len(rows)} | {statistics.fmean(r['live'] for r in rows):.4f} |")
    shadow_rows = []
    for t in TYPES:
        of_type = [r for r in scored if r["type"] == t]
        for name in sorted({name for r in of_type for name in r.get("shadow", {})}):
            pairs = [(r["live"], r["shadow"][name]) for r in of_type if name in r.get("shadow", {})]
            shadow_rows.append(
                f"| {t} | {name} | {len(pairs)} | {statistics.fmean(s for _, s in pairs):.4f} | "
                f"{statistics.fmean(s - live for live, s in pairs):+.4f} |"
            )
    if shadow_rows:
        lines += ["", "**Shadow variants (same questions as live, never submitted):**", "",
                  "| Type | Variant | Questions | Mean log score | Mean diff vs live |", "|---|---|---|---|---|",
                  *shadow_rows]
    if not scored:
        lines.append("| (none resolved yet) | 0 | |")
    return "\n".join(lines)


# ---------------------------------------------------------------- per model (pure)

# When the frontier lineup (Claude) went live: an ISO UTC datetime string, e.g.
# "2026-10-25T00:00:00+00:00". None = not switched on yet: everything is "before".
FRONTIER_SWITCH_ON: str | None = None
PERIODS = ("before", "since")
PERIOD_TITLES = {"before": "Before frontier switch-on", "since": "Since frontier switch-on"}
# frontier.AGENT: the agent slot's ledger name (the call is Opus 5.5 with web tools).
AGENT_MODEL = "anthropic/claude-opus-5-5-agent"
MODEL_GROUPS = ("agent", "claude", "flash", "other")
# Binary subset medians, through the live final steps (forecast_safety.adjust_binary).
SUBSETS = {
    "claude-only": lambda name: model_group(name) in ("agent", "claude"),
    "flash-only": lambda name: model_group(name) == "flash",
    "no-agent": lambda name: model_group(name) != "agent",
}


def model_group(name: str) -> str:
    """'agent' / 'claude' (Opus 5.5, Opus 5, Haiku 5.5 backups) / 'flash' (each
    Gemini Flash model, free key or paid, Flash-Lite too) / 'other'."""
    if name == AGENT_MODEL:
        return "agent"
    if name.startswith("anthropic/") or "claude" in name:
        return "claude"
    if "gemini" in name and "flash" in name:
        return "flash"
    return "other"


def forecast_model(entry: dict) -> str | None:
    """The model whose forecast this log entry is: 'forecast_model' (frontier
    logs), else the first model that answered (older logs, reused forecasts)."""
    name = entry.get("forecast_model") or next(iter(entry.get("answered_models") or []), None)
    return str(name).removeprefix("replay/") if name else None


def period_of(when: str | None, switch_on: str | None = None) -> str:
    """'before' / 'since' the frontier switch-on, from a forecast's ISO time."""
    switch_on = FRONTIER_SWITCH_ON if switch_on is None else switch_on
    if not switch_on or not when:
        return "before"
    try:
        moment = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
        start = datetime.fromisoformat(switch_on.replace("Z", "+00:00"))
    except ValueError:
        return "before"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return "since" if moment >= start else "before"


def model_forecasts(record: dict) -> list[tuple[str, Any, str | None]]:
    """(model, parsed forecast, time) of each finished forecast in the log."""
    out = []
    for entry in record.get("forecasts") or []:
        name = forecast_model(entry)
        if entry.get("status") == "ok" and name and entry.get("parsed") is not None:
            out.append((name, entry["parsed"], entry.get("started_at") or record.get("started_at")))
    return out


def subset_forecast(values: list[float]) -> float | None:
    """A subset's binary forecast through the live final steps (median, stretch
    at its live value, extreme check, 2-98% clip); None if empty."""
    from forecast_safety import NoValidForecast, adjust_binary

    try:
        return adjust_binary(values)
    except NoValidForecast:
        return None


def score_models(records: list[dict], resolution_of, switch_on: str | None = None) -> list[dict]:  # type: ignore[no-untyped-def]
    """Per resolved question: {"type", "period" (of the live forecast), "live",
    "models": [(model, period, score)], "subsets": {name: score} (binary)}."""
    rows = []
    for record in records:
        question = record.get("question") or {}
        resolution = resolution_of(question.get("id_of_post"))
        if not resolution:
            continue
        kind = question.get("question_type")
        live = live_score(record, resolution)
        if live is None:
            continue
        row = {"type": kind, "period": period_of(record.get("started_at"), switch_on), "live": live,
               "models": [], "subsets": {}}
        forecasts = model_forecasts(record)
        for name, parsed, when in forecasts:
            try:
                score = forecast_score(kind, parsed, resolution)
            except (TypeError, ValueError):
                score = None
            if score is not None:
                row["models"].append((name, period_of(when, switch_on), score))
        if kind == "binary":
            for subset, wanted in SUBSETS.items():
                values = [float(p) for name, p, _ in forecasts if wanted(name) and isinstance(p, (int, float))]
                p = subset_forecast(values)
                if p is not None:
                    row["subsets"][subset] = forecast_score(kind, p, resolution)
        rows.append(row)
    return rows


def per_model_markdown(rows: list[dict], switch_on: str | None = None) -> str:
    """Per-model and binary-subset tables, before / since the frontier switch-on.
    Private (fall26-data only): never printed to the job log or page."""
    switch_on = FRONTIER_SWITCH_ON if switch_on is None else switch_on
    lines = [f"Frontier switch-on: {switch_on or 'not set (everything is before)'}. "
             "Each model alone on the questions it forecast; diff vs live on the same questions."]
    for period in PERIODS:
        lines += ["", f"**{PERIOD_TITLES[period]}:**", ""]
        model_rows = []
        for kind in TYPES:
            of_type = [r for r in rows if r["type"] == kind]
            names = sorted({m for r in of_type for m, p, _ in r["models"] if p == period},
                           key=lambda m: (MODEL_GROUPS.index(model_group(m)), m))
            for name in names:
                pairs = [(r["live"], s) for r in of_type for m, p, s in r["models"] if m == name and p == period]
                model_rows.append(
                    f"| {kind} | {name} | {model_group(name)} | {len(pairs)} | "
                    f"{statistics.fmean(s for _, s in pairs):.4f} | {statistics.fmean(s - live for live, s in pairs):+.4f} |"
                )
        if model_rows:
            lines += ["| Type | Model | Group | Forecasts | Mean log score | Mean diff vs live |",
                      "|---|---|---|---|---|---|", *model_rows]
        else:
            lines.append("No resolved per-model forecasts.")
        binary = [r for r in rows if r["type"] == "binary" and r["period"] == period]
        subset_rows = []
        for subset in SUBSETS:
            pairs = [(r["live"], r["subsets"][subset]) for r in binary if subset in r["subsets"]]
            if pairs:
                subset_rows.append(
                    f"| {subset} | {len(pairs)} | {statistics.fmean(s for _, s in pairs):.4f} | "
                    f"{statistics.fmean(s - live for live, s in pairs):+.4f} |"
                )
        if subset_rows:
            lines += ["", "Binary subset medians (live final steps: median, stretch, extreme check, 2-98% clip):", "",
                      "| Subset | Questions | Mean log score | Mean diff vs live |", "|---|---|---|---|", *subset_rows]
    return "\n".join(lines)


# ---------------------------------------------------------------- data


def _github(path: str, token: str, method: str = "GET", body: dict | None = None) -> Any:
    response = requests.request(
        method,
        f"https://api.github.com/repos/{DATA_REPO}/{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json=body,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


SHADOWS_SUFFIX = "_shadows.json"


def merge_shadow_files(files: dict[str, dict]) -> list[dict]:
    """Question logs (path -> record) with each '<log>_shadows.json' (the
    end-of-run shadow forecaster, run timing) merged into its log."""
    records = {path: record for path, record in files.items() if not path.endswith(SHADOWS_SUFFIX)}
    for path, extra in files.items():
        if not path.endswith(SHADOWS_SUFFIX):
            continue
        record = records.get(path.removesuffix(SHADOWS_SUFFIX) + ".json")
        if record is None:
            continue
        if "shadow_model" in extra:
            record["shadow_model"] = extra["shadow_model"]
        record.setdefault("shadow", {}).update(extra.get("shadow") or {})
    return list(records.values())


def all_tournament_records(token: str) -> list[dict]:
    """Every tournament question log (every run's attempt), shadow files merged in."""
    tree = _github("git/trees/HEAD?recursive=1", token)["tree"]
    paths = [t["path"] for t in tree if t["path"].startswith("questions/tournament/") and t["path"].endswith(".json")]
    return merge_shadow_files(
        {path: json.loads(base64.b64decode(_github(f"contents/{path}", token)["content"])) for path in paths}
    )


def latest_submitted_records(token: str, records: list[dict] | None = None) -> list[dict]:
    """One record per question: the latest submitted tournament log."""
    latest: dict[Any, dict] = {}
    for record in records if records is not None else all_tournament_records(token):
        post = record.get("question", {}).get("id_of_post")
        if record.get("submitted") and (post not in latest or record.get("started_at", "") > latest[post].get("started_at", "")):
            latest[post] = record
    return list(latest.values())


def score_records(records: list[dict], resolution_of) -> list[dict]:  # type: ignore[no-untyped-def]
    scored = []
    for record in records:
        resolution = resolution_of(record["question"].get("id_of_post"))
        if not resolution:
            continue
        live = live_score(record, resolution)
        if live is None:
            continue
        row = {"type": record["question"].get("question_type"), "live": live, "shadow": {}}
        for name, forecast in (record.get("shadow") or {}).items():
            score = forecast_score(row["type"], forecast, resolution)
            if score is not None:
                row["shadow"][name] = score
        scored.append(row)
    return scored


def resolution_lookup():  # type: ignore[no-untyped-def]
    """post id -> the Metaculus resolution string (or None), cached. Uses the
    bot's METACULUS_TOKEN (forecasting_tools)."""
    from forecasting_tools import MetaculusClient

    client = MetaculusClient()
    cache: dict[Any, str | None] = {}

    def resolution_of(post: Any) -> str | None:
        if post not in cache:
            question = client.get_question_by_post_id(post)
            if isinstance(question, list):
                question = question[0]
            cache[post] = getattr(question, "resolution_string", None)
        return cache[post]

    return resolution_of


def save_markdown(path: str, text: str, token: str, message: str) -> None:
    """Write (or overwrite) a markdown file in fall26-data."""
    body = {"message": message, "content": base64.b64encode(text.encode()).decode()}
    try:
        body["sha"] = _github(f"contents/{path}", token)["sha"]
    except requests.HTTPError:
        pass
    _github(f"contents/{path}", token, method="PUT", body=body)


def lab_section(token: str) -> str:
    """The replay lab's paired results so far (lab/results.json), or a note."""
    try:
        rows = json.loads(base64.b64decode(_github("contents/lab/results.json", token)["content"]))
    except Exception:
        return "\n**Replay lab:** no results yet.\n"
    import lab

    results = [lab.LabResult(r["post"], r["type"], r["experiment"], r.get("diff"), r.get("note", "")) for r in rows]
    return "\n" + lab.report(results) + "\n"


def main() -> None:
    from bot_helpers import silence_noisy_dependencies

    silence_noisy_dependencies()
    token = os.environ["DATA_REPO_TOKEN"]
    resolution_of = resolution_lookup()
    every = all_tournament_records(token)
    records = latest_submitted_records(token, every)
    scored = score_records(records, resolution_of)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report = (
        f"## Scoreboard {stamp}\n\n{len(records)} forecast question(s) logged, "
        f"{len(scored)} resolved and scored.\n\n{report_markdown(scored)}\n\n"
        f"**Shadow forecaster answer rate (never submitted):**\n\n{shadow_answer_rate(every)}\n"
    )
    report += lab_section(token)
    print(report)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report)
    # Per-model and subset tables: only in the saved fall26-data file, never on the job page.
    private = f"\n**Per model and binary subsets (private):**\n\n{per_model_markdown(score_models(records, resolution_of))}\n"
    save_markdown(f"scoreboard/{stamp}.md", report + private, token, f"Scoreboard {stamp}")


if __name__ == "__main__":
    main()
