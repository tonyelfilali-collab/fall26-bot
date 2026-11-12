"""
Aggregation mode (architect, 8 Nov; Tier C, built OFF): which finished forecasts
are combined into the submitted one. bot_config.AGGREGATION_MODE.

- "all" (live today): every finished forecast.
- "claude_first": if at least CLAUDE_FIRST_MIN (2) Claude Opus forecasts were
  read (agent1, agent2, plain Opus 5.5, the Opus fallbacks), ONLY those are
  combined, the same way as now (binary median, multiple choice per-option
  median + floor, numeric pointwise median + 5% uniform), then the same
  clipping and checks. Fewer than 2: everything, as now. Flash keeps running
  and is saved and scored (the question log keeps every forecast).
  Fable 5.2 (12 Nov) counts as a Claude forecast.
  Haiku (the last Claude backup) does not count as a Claude forecast here.

Pre-registered switch-on rule (architect, 8 Nov), checked by the Scoreboard on
each run (scoreboard.py -> status/claude_first.json in fall26-data; the daily
health check reports it):
- at least RULE_MIN_QUESTIONS (25) agent-era resolved questions (live forecast
  made since the frontier switch-on);
- "claude_first" (as above, simulated on the saved forecasts) beats the
  submitted forecast by more than RULE_MIN_GAIN_ALL (0.10) log score per
  question, all types;
- and is not worse than RULE_MIN_GAIN_BINARY (-0.05) per question on binary.
All three: the one-line PR setting AGGREGATION_MODE = "claude_first" is
pre-approved (merge after a green live run, tell the architect).
"""
from __future__ import annotations

import statistics
from typing import Any, Callable, Iterable

MODES = ("all", "claude_first")
CLAUDE_FIRST_MIN = 2

# The forecast kinds the live combine sees (lab._live_predictions).
LIVE_KINDS = ("planned", "round2", "quick", "emergency", "backup")

RULE_MIN_QUESTIONS = 25
RULE_MIN_GAIN_ALL = 0.10
RULE_MIN_GAIN_BINARY = -0.05
STATUS_PATH = "status/claude_first.json"


def is_claude_forecast(model: str | None) -> bool:
    """A Claude Opus or Fable forecast: agent1, agent2, plain Opus 5.5, Opus 5 (a
    fallback), Fable 5.2 (12 Nov), on the Anthropic API or in a replay ("replay/" prefix)."""
    name = str(model or "").removeprefix("replay/")
    return "claude-opus" in name or "claude-fable" in name


def choose(models: list[str | None], mode: str) -> tuple[list[int], str]:
    """(indexes of the forecasts to combine, "claude" or "all")."""
    if mode not in MODES:
        raise ValueError(f"unknown aggregation mode {mode!r} (allowed: {', '.join(MODES)})")
    if mode == "claude_first":
        claude = [i for i, model in enumerate(models) if is_claude_forecast(model)]
        if len(claude) >= CLAUDE_FIRST_MIN:
            return claude, "claude"
    return list(range(len(models))), "all"


# ---------------------------------------------------------------- the switch-on rule


def record_models_and_values(record: dict) -> list[tuple[str | None, Any]]:
    """(model, parsed forecast) of each finished forecast in a question log, in
    log order (the forecasts the live combine saw)."""
    out = []
    for entry in record.get("forecasts") or []:
        if entry.get("status") != "ok" or entry.get("parsed") is None or entry.get("kind") not in LIVE_KINDS:
            continue
        model = entry.get("forecast_model") or next(iter(entry.get("answered_models") or []), None)
        out.append((model or entry.get("planned_model"), entry["parsed"]))
    return out


def combine_saved(snapshot: dict, values: list[Any]) -> Any:
    """The live combine (lab.combine: binary median + final steps, multiple
    choice per-option median + floor, numeric pointwise median + 5% uniform)
    of saved forecasts, in the saved JSON form."""
    import lab
    from main import _saved_prediction
    from question_log import to_jsonable

    question = lab._question_object(snapshot)
    predictions = [_saved_prediction(question, {"value": v, "reasoning": ""}).prediction_value for v in values]
    return to_jsonable(lab.combine(question, predictions))


def rule_rows(records: Iterable[dict], resolution_of: Callable[[Any], str | None],
              switch_on: str | None = None, combine: Callable[[dict, list], Any] = combine_saved) -> list[dict]:
    """One row per agent-era resolved question (latest submitted log):
    {"post", "type", "submitted", "claude_first", "used", "claude"}.
    "claude_first" = the score claude_first would have had: the Claude-only
    combine when it had 2+ Claude forecasts, else the submitted forecast."""
    from scoreboard import forecast_score, live_score, period_of

    rows = []
    for record in records:
        question = record.get("question") or {}
        if period_of(record.get("started_at"), switch_on) != "since":
            continue
        resolution = resolution_of(question.get("id_of_post"))
        if not resolution:
            continue
        submitted = live_score(record, resolution)
        if submitted is None:
            continue
        pairs = record_models_and_values(record)
        chosen, used = choose([m for m, _ in pairs], "claude_first")
        score = submitted
        if used == "claude":
            try:
                score = forecast_score(question.get("question_type"), combine(question, [pairs[i][1] for i in chosen]), resolution)
            except Exception:
                score, used = submitted, "all (Claude-only combine failed)"
            if score is None:
                score, used = submitted, "all (Claude-only combine not scorable)"
        rows.append({"post": question.get("id_of_post"), "type": question.get("question_type"),
                     "submitted": submitted, "claude_first": score, "used": used,
                     "claude": sum(1 for m, _ in pairs if is_claude_forecast(m))})
    return rows


def rule_result(rows: list[dict]) -> dict:
    """The three conditions on the rule rows (per-question mean differences,
    claude_first minus submitted; + = claude_first better)."""
    binary = [r for r in rows if r["type"] == "binary"]

    def gain(of: list[dict]) -> float | None:
        return statistics.fmean(r["claude_first"] - r["submitted"] for r in of) if of else None

    gain_all, gain_binary = gain(rows), gain(binary)
    conditions = {
        "questions": len(rows) >= RULE_MIN_QUESTIONS,
        "gain_all": gain_all is not None and gain_all > RULE_MIN_GAIN_ALL,
        "gain_binary": gain_binary is None or gain_binary >= RULE_MIN_GAIN_BINARY,
    }
    return {
        "questions": len(rows),
        "claude_only_questions": sum(1 for r in rows if r["used"] == "claude"),
        "binary_questions": len(binary),
        "gain_all": gain_all,
        "gain_binary": gain_binary,
        "conditions": conditions,
        "met": all(conditions.values()),
    }


def rule_markdown(result: dict, rows: list[dict]) -> str:
    """Private (fall26-data only): the rule's numbers and one row per question."""
    def num(x: float | None) -> str:
        return "n/a" if x is None else f"{x:+.4f}"

    c = result["conditions"]
    lines = [
        "**claude_first switch-on rule (pre-registered 8 Nov):**", "",
        f"- Agent-era resolved questions: {result['questions']} (need {RULE_MIN_QUESTIONS}): {'yes' if c['questions'] else 'no'}",
        f"- claude_first minus submitted, all types: {num(result['gain_all'])} per question "
        f"(need > +{RULE_MIN_GAIN_ALL:.2f}): {'yes' if c['gain_all'] else 'no'}",
        f"- Binary ({result['binary_questions']}): {num(result['gain_binary'])} per question "
        f"(need >= {RULE_MIN_GAIN_BINARY:+.2f}): {'yes' if c['gain_binary'] else 'no'}",
        f"- Claude-only combine used on {result['claude_only_questions']} question(s) (2+ Claude forecasts); the rest = submitted",
        f"- **Rule {'MET' if result['met'] else 'not met'}**",
        "", "| Post | Type | Claude forecasts | Used | Submitted | claude_first | Diff |", "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['post']} | {r['type']} | {r['claude']} | {r['used']} | {r['submitted']:.4f} | "
                     f"{r['claude_first']:.4f} | {r['claude_first'] - r['submitted']:+.4f} |")
    return "\n".join(lines)


def health_line(status: dict | None, mode_now: str) -> tuple[str, bool]:
    """(the daily report's line, whether it is a warning). Public: counts and
    yes/no only, never scores (those stay in fall26-data)."""
    if mode_now == "claude_first":
        return "Aggregation mode: claude_first (switched on)", False
    if not status:
        return "claude_first rule: not checked yet (the Scoreboard writes it)", False
    c = status.get("conditions") or {}

    def yn(key: str) -> str:
        return "yes" if c.get(key) else "no"

    detail = (f"{status.get('questions', 0)} of {RULE_MIN_QUESTIONS} agent-era questions; "
              f"all types > +{RULE_MIN_GAIN_ALL:.2f}: {yn('gain_all')}; binary >= {RULE_MIN_GAIN_BINARY:+.2f}: {yn('gain_binary')}; "
              f"checked {str(status.get('checked_at', '?'))[:10]}")
    if status.get("met"):
        return f"claude_first rule MET ({detail}): open the one-line switch PR (pre-approved), merge after a green live run, tell the architect", True
    return f"claude_first rule not met ({detail})", False
