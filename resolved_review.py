"""
Resolved review (architect, 2 Nov): one row per resolved Fall question (33121),
worst first: our Metaculus peer score, when we forecast it (before / since
the frontier switch-on) and which models answered, our forecast (binary p,
MC top option, numeric median and 10-90%) vs the outcome, and a check that the
forecast SUBMITTED on Metaculus is the one in our private log (binary upside
down, MC options in the wrong order, numeric bounds / open ends / CDF).

Read-only: forecasts nothing, spends nothing. The table and the raw details go
to fall26-data (reviews/) only; the job log shows counts and the path only.

    poetry run python resolved_review.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import requests

from scoreboard import FRONTIER_SWITCH_ON, _github, period_of, save_markdown

API = "https://www.metaculus.com/api"
TOURNAMENT = 33121  # fall-futureeval-2026
MATCH = 0.005  # binary / MC: submitted vs log, max probability difference
CDF_MATCH = 0.03  # numeric: max CDF difference (the library standardizes the tails)


# ---------------------------------------------------------------- pure helpers
def when(value: Any) -> datetime | None:
    """Metaculus times come as epoch seconds or ISO strings."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def scored_entry(history: list[dict], scoring_time: datetime | None) -> dict | None:
    """Our forecast standing at the scoring time (else the last one)."""
    standing = None
    for entry in sorted(history, key=lambda e: when(e.get("start_time")) or datetime.min.replace(tzinfo=timezone.utc)):
        start, end = when(entry.get("start_time")), when(entry.get("end_time"))
        if scoring_time is None or (start and start <= scoring_time and (end is None or end > scoring_time)):
            standing = entry
    if standing is None and history:
        standing = history[-1]
    return standing


def peer_score(score_data: dict | None) -> float | None:
    if not isinstance(score_data, dict):
        return None
    for key in ("spot_peer_score", "peer_score"):
        if isinstance(score_data.get(key), (int, float)):
            return float(score_data[key])
    return None


def grid_values(scaling: dict, size: int) -> list[float]:
    """The real value at each of the CDF's points (linear, or log with a zero point)."""
    low, high, zero = scaling["range_min"], scaling["range_max"], scaling.get("zero_point")
    xs = [i / (size - 1) for i in range(size)]
    if zero is None:
        return [low + x * (high - low) for x in xs]
    ratio = (high - zero) / (low - zero)
    return [low + (high - low) * (ratio**x - 1) / (ratio - 1) for x in xs]


def quantile(values: list[float], cdf: list[float], q: float) -> float | None:
    for i in range(1, len(cdf)):
        if cdf[i] >= q:
            span = cdf[i] - cdf[i - 1]
            t = 0.0 if span <= 0 else (q - cdf[i - 1]) / span
            return values[i - 1] + t * (values[i] - values[i - 1])
    return None


def log_cdf_at(declared: list[dict], x: float) -> float:
    """Our log's declared percentiles, read at a value (flat outside)."""
    points = sorted((p["value"], p["percentile"]) for p in declared)
    if x <= points[0][0]:
        return points[0][1]
    for (v0, p0), (v1, p1) in zip(points, points[1:]):
        if x <= v1:
            return p0 if v1 == v0 else p0 + (x - v0) / (v1 - v0) * (p1 - p0)
    return points[-1][1]


def summary(kind: str, values: Any, question: dict) -> str:
    if not values and values != 0:
        return "-"
    if kind == "binary":
        return f"{values:.2f}"
    if kind == "multiple_choice":
        best = max(values, key=lambda o: o[1])
        return f"top: {best[0]} {best[1]:.2f}"

    def num(v):  # type: ignore[no-untyped-def]
        return "outside the range" if v is None else f"{v:.4g}"

    med, p10, p90 = values
    return f"median {num(med)} (10-90%: {num(p10)} to {num(p90)})"


def submitted_values(kind: str, entry: dict | None, question: dict) -> Any:
    """What Metaculus has: binary p(yes); MC [(option, p)] in the question's
    option order; numeric (median, 10%, 90%) and the raw CDF."""
    if not entry:
        return None
    forecast = entry.get("forecast_values")
    if kind == "binary":
        if isinstance(forecast, list) and len(forecast) == 2:
            return float(forecast[1])
        return entry.get("probability_yes")
    if kind == "multiple_choice":
        return list(zip(question.get("options") or [], forecast or []))
    if not forecast:
        return None
    grid = grid_values(question["scaling"], len(forecast))
    return tuple(quantile(grid, forecast, q) for q in (0.5, 0.1, 0.9))


def log_values(kind: str, final: Any) -> Any:
    if final is None:
        return None
    if kind == "binary":
        return float(final)
    if kind == "multiple_choice":
        return [(o["option_name"], o["probability"]) for o in final.get("predicted_options", [])]
    points = sorted((p["value"], p["percentile"]) for p in final.get("declared_percentiles") or [])
    if len(points) < 2:
        return None
    values, cdf = [p[0] for p in points], [p[1] for p in points]
    return tuple(quantile(values, cdf, q) for q in (0.5, 0.1, 0.9))


def submission_check(kind: str, entry: dict | None, question: dict, log: dict | None) -> str:
    """'same', or what differs between Metaculus and our log."""
    if not entry:
        return "no forecast on Metaculus"
    if not log:
        return "no private log"
    final = log.get("final_forecast")
    if kind == "binary":
        sent, meant = submitted_values(kind, entry, question), log_values(kind, final)
        if sent is None or meant is None:
            return "unreadable"
        if abs(sent - meant) <= MATCH:
            return "same"
        if abs(sent - (1 - meant)) <= MATCH:
            return f"UPSIDE DOWN (sent {sent:.3f}, log {meant:.3f})"
        return f"DIFFERENT (sent {sent:.3f}, log {meant:.3f})"
    if kind == "multiple_choice":
        sent = dict(submitted_values(kind, entry, question) or [])
        meant = dict(log_values(kind, final) or [])
        if set(sent) != set(meant):
            return f"OPTION NAMES DIFFER ({sorted(set(sent) ^ set(meant))})"
        worst = max(abs(sent[o] - meant[o]) for o in sent)
        return "same" if worst <= MATCH else f"DIFFERENT (max option gap {worst:.3f}: order?)"
    problems = []
    scaling = question.get("scaling") or {}
    lq = log.get("question") or {}
    for ours, theirs, label in ((lq.get("lower_bound"), scaling.get("range_min"), "lower bound"),
                                (lq.get("upper_bound"), scaling.get("range_max"), "upper bound"),
                                (lq.get("zero_point"), scaling.get("zero_point"), "zero point"),
                                (lq.get("open_lower_bound"), question.get("open_lower_bound"), "open lower end"),
                                (lq.get("open_upper_bound"), question.get("open_upper_bound"), "open upper end")):
        if ours != theirs and not (isinstance(ours, (int, float)) and isinstance(theirs, (int, float)) and abs(ours - theirs) < 1e-9):
            problems.append(f"{label}: log {ours} vs Metaculus {theirs}")
    cdf = entry.get("forecast_values") or []
    declared = (final or {}).get("declared_percentiles") or []
    if cdf and declared and scaling:
        grid = grid_values(scaling, len(cdf))
        gap = max(abs(c - log_cdf_at(declared, x)) for c, x in zip(cdf, grid))
        if gap > CDF_MATCH:
            problems.append(f"CDF differs (max gap {gap:.3f})")
    return "same" if not problems else "; ".join(problems)


def outcome_text(kind: str, question: dict) -> str:
    resolution = question.get("resolution")
    return "-" if resolution is None else str(resolution)


def models_answered(log: dict | None) -> str:
    if not log:
        return "-"
    names = [f.get("forecast_model") for f in log.get("forecasts") or [] if f.get("status") == "ok"]
    return ", ".join(n.split("/")[-1] for n in names if n) or "-"


def build_rows(posts: list[dict], logs_of) -> list[dict]:  # type: ignore[no-untyped-def]
    rows = []
    for post in posts:
        question = post.get("question")
        if not question:
            continue
        kind = question.get("type")
        mine = question.get("my_forecasts") or {}
        scoring_time = when(question.get("spot_scoring_time") or question.get("cp_reveal_time")
                            or question.get("actual_close_time") or question.get("scheduled_close_time"))
        entry = scored_entry(mine.get("history") or [], scoring_time)
        logs = [r for r in logs_of(post["id"]) if r.get("submitted")]
        before = [r for r in logs if scoring_time is None or (when(r.get("started_at")) or scoring_time) <= scoring_time]
        log = max(before or logs, key=lambda r: r.get("started_at", ""), default=None)
        rows.append({
            "post": post["id"],
            "title": post.get("title") or question.get("title"),
            "type": kind,
            "peer": peer_score(mine.get("score_data")),
            "score_data_keys": sorted((mine.get("score_data") or {}).keys()),
            "forecast_at": log.get("started_at") if log else None,
            "period": period_of(log.get("started_at")) if log else "-",
            "models": models_answered(log),
            "ours": summary(kind, submitted_values(kind, entry, question), question),
            "log": summary(kind, log_values(kind, (log or {}).get("final_forecast")), question),
            "outcome": outcome_text(kind, question),
            "check": submission_check(kind, entry, question, log),
            "log_count": len(logs),
        })
    return sorted(rows, key=lambda r: (r["peer"] is None, r["peer"] if r["peer"] is not None else 0))


def markdown(rows: list[dict], now: datetime) -> str:
    lines = [
        f"# Resolved Fall questions ({TOURNAMENT}), worst first ({now:%Y-%m-%d %H:%M} UTC)",
        "",
        f"Peer score = Metaculus's own score for our forecast. Period: before / since the frontier switch-on ({FRONTIER_SWITCH_ON}).",
        "Ours = the forecast Metaculus has at scoring time; Check = Metaculus vs our private log.",
        "",
        "| Post | Type | Peer | Forecast at | Period | Models answered | Ours (Metaculus) | Outcome | Check |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        peer = "-" if r["peer"] is None else f"{r['peer']:.1f}"
        lines.append(f"| [{r['post']}](https://www.metaculus.com/questions/{r['post']}) | {r['type']} | {peer} | "
                     f"{(r['forecast_at'] or '-')[:16]} | {r['period']} | {r['models']} | {r['ours']} | {r['outcome']} | {r['check']} |")
    total = sum(r["peer"] for r in rows if r["peer"] is not None)
    lines += ["", f"Sum of peer scores: {total:.1f} over {sum(r['peer'] is not None for r in rows)} scored question(s)."]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- I/O
def resolved_posts(get=requests.get) -> list[dict]:  # type: ignore[no-untyped-def]
    posts, offset = [], 0
    headers = {"Authorization": f"Token {os.environ['METACULUS_TOKEN']}"}
    while True:
        response = get(f"{API}/posts/", params={"tournaments": TOURNAMENT, "statuses": "resolved", "limit": 100,
                                                "offset": offset, "with_cp": "true"}, headers=headers, timeout=60)
        response.raise_for_status()
        data = response.json()
        results = data.get("results", [])
        posts += results
        if not data.get("next") or not results:
            return posts
        offset += len(results)


def log_reader(token: str):  # type: ignore[no-untyped-def]
    tree = _github("git/trees/HEAD?recursive=1", token)["tree"]
    paths = [t["path"] for t in tree if t["path"].startswith("questions/tournament/")
             and t["path"].endswith(".json") and not t["path"].endswith("_shadows.json")]

    def logs_of(post: int) -> list[dict]:
        mine = [p for p in paths if p.rsplit("/", 1)[-1].startswith(f"{post}_")]
        return [json.loads(base64.b64decode(_github(f"contents/{p}", token)["content"])) for p in mine]

    return logs_of


def main() -> None:
    now = datetime.now(timezone.utc)
    token = os.environ["DATA_REPO_TOKEN"]
    posts = resolved_posts()
    rows = build_rows(posts, log_reader(token))
    base = f"reviews/resolved_{now:%Y-%m-%d_%H%M}"
    save_markdown(f"{base}.md", markdown(rows, now), token, "Resolved review (private)")
    save_markdown(f"{base}.json", json.dumps(rows, indent=1), token, "Resolved review details (private)")
    # Counts and the path only: never scores, forecasts or outcomes on the public job page.
    flagged = sum(r["check"] not in ("same", "no forecast on Metaculus") for r in rows)
    keys = sorted({k for r in rows for k in r["score_data_keys"]})
    print(f"Resolved posts: {len(posts)}; rows: {len(rows)}; with a peer score: {sum(r['peer'] is not None for r in rows)}; "
          f"submission check flagged: {flagged}")
    print(f"score_data fields seen: {', '.join(keys) or 'none'}")
    print(f"Saved to fall26-data {base}.md and .json")


if __name__ == "__main__":
    sys.exit(main())
