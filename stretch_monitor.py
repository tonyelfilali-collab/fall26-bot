"""
Stretch monitor (architect, 16 Nov; Tier A, monitor only, no live change).

On agent-era resolved BINARY questions (the latest submitted log started at
or after scoreboard.FRONTIER_SWITCH_ON), score "bolder" forecasts:
- applied: the submitted p stretched in log-odds by k (logit(p') = k * logit(p),
  then the live 2-98% clip; calibration.stretched), k = 1.2 and 1.5, against
  the submitted p (k = 1.0);
- shadow: the logged free shadows "stretch-1.2" / "stretch-1.5" (shadow.py: the
  stretch of the every-forecast median; since claude_first that base can
  differ from what was submitted), where the log has them;
- leave-one-out on "applied": each question scored with the best k (of 1.0,
  1.2, 1.5) on all the others (calibration.leave_one_out), so picking k is
  not scored on the same question.

Every Scoreboard run saves the numbers to fall26-data status/stretch_monitor.json
and its private report. At REPORT_AT (30) questions the daily health check
warns "report due" (Claude Code reports to the architect). The public line has
counts only, never scores or forecasts.
"""
from __future__ import annotations

import statistics
from typing import Any, Callable, Iterable

GRID = (1.0, 1.2, 1.5)
SHADOWS = {1.2: "stretch-1.2", 1.5: "stretch-1.5"}
REPORT_AT = 30
STATUS_PATH = "status/stretch_monitor.json"


def rows(records: Iterable[dict], resolution_of: Callable[[Any], str | None],
         switch_on: str | None = None) -> list[dict]:
    """One row per agent-era resolved binary question: {"post", "p", "yes", "shadow": {k: p}}."""
    from scoreboard import period_of

    out = []
    for record in records:
        question = record.get("question") or {}
        if question.get("question_type") != "binary" or period_of(record.get("started_at"), switch_on) != "since":
            continue
        resolution = (resolution_of(question.get("id_of_post")) or "").lower()
        p = record.get("final_forecast")
        if resolution not in ("yes", "no") or not isinstance(p, (int, float)) or isinstance(p, bool):
            continue
        logged = record.get("shadow") or {}
        shadow = {k: float(logged[name]) for k, name in SHADOWS.items()
                  if isinstance(logged.get(name), (int, float)) and not isinstance(logged.get(name), bool)}
        out.append({"post": question.get("id_of_post"), "p": float(p), "yes": resolution == "yes", "shadow": shadow})
    return out


def result(table: list[dict]) -> dict:
    """Mean log scores per question: submitted, applied k, logged shadows, leave-one-out. Gains vs submitted."""
    from calibration import leave_one_out, stretch_total
    from scoreboard import binary_score

    n = len(table)
    out: dict = {"questions": n, "report_at": REPORT_AT, "report_due": n >= REPORT_AT}
    if not n:
        return out
    pairs = [(r["p"], r["yes"]) for r in table]
    submitted = stretch_total(pairs, 1.0) / n
    out["submitted"] = submitted
    out["applied_gain"] = {f"{k:g}": stretch_total(pairs, k) / n - submitted for k in GRID if k != 1.0}
    shadow_gain = {}
    for k in SHADOWS:
        have = [r for r in table if k in r["shadow"]]
        if have:
            shadow_gain[f"{k:g}"] = {"questions": len(have), "gain": statistics.fmean(
                binary_score(r["shadow"][k], r["yes"]) - binary_score(r["p"], r["yes"]) for r in have)}
    out["shadow_gain"] = shadow_gain
    if n >= 2:
        scores, picks = leave_one_out(pairs, GRID)
        out["loo_gain"] = statistics.fmean(scores) - submitted
        out["loo_picks"] = {f"{k:g}": count for k, count in picks.items()}
    return out


def markdown(res: dict) -> str:
    n = res.get("questions", 0)
    lines = ["**Stretch monitor (architect, 16 Nov; monitor only, no live change):** agent-era resolved binary: "
             f"{n} (report at {REPORT_AT}). Mean log score per question vs submitted (+ = bolder is better)."]
    if not n:
        return "\n".join(lines + ["", "No agent-era binary question has resolved yet."])
    lines += ["", "| Variant | Questions | Gain vs submitted |", "|---|---|---|"]
    for k, gain in res["applied_gain"].items():
        lines.append(f"| submitted stretched k={k} | {n} | {gain:+.4f} |")
    for k, item in res.get("shadow_gain", {}).items():
        lines.append(f"| logged shadow stretch-{k} | {item['questions']} | {item['gain']:+.4f} |")
    if "loo_gain" in res:
        picks = ", ".join(f"k={k} {c}x" for k, c in res["loo_picks"].items())
        lines.append(f"| leave-one-out best k (picked: {picks}) | {n} | {res['loo_gain']:+.4f} |")
    return "\n".join(lines)


def health_line(status: dict | None) -> tuple[str, bool]:
    """(line, warn). Counts only (public)."""
    if not status:
        return "Stretch monitor: not checked yet (the Scoreboard writes it)", False
    n = status.get("questions", 0)
    checked = str(status.get("checked_at", ""))[:10]
    if n >= REPORT_AT:
        return (f"Stretch monitor: {n} agent-era binary questions resolved (report at {REPORT_AT}): "
                f"report due: tell the architect (checked {checked})"), True
    return f"Stretch monitor: {n} of {REPORT_AT} agent-era binary questions resolved (checked {checked})", False
