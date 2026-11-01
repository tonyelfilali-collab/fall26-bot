"""Kimi no-search answers (architect, 1 Nov): a Kimi shadow reply that cited 0
domains counts as "no search"; the Scoreboard scores Kimi on all answers and on
searched answers only, and counts the no-search ones; the daily report shows
"Kimi: N calls, M without search, K failed". Fake records only (0 real calls)."""
from __future__ import annotations

import health_check
import claude_health
import scoreboard

SAVED = {"status": "saved", "domains_count": 6, "metaculus_cited": False}
NO_SEARCH = {"status": "saved", "domains_count": 0, "metaculus_cited": False}  # 46283, 46291
FAILED = {"status": "failed (NotFoundError)", "domains_count": None, "metaculus_cited": None}  # 46287
TIMED_OUT = {"status": "timed out"}
SKIPPED = {"status": "skipped: daily cap"}
UNREADABLE = {"status": "no answer", "domains_count": 3}


def test_kimi_counts_calls_no_search_and_failures():
    entries = [SAVED, NO_SEARCH, NO_SEARCH, FAILED, TIMED_OUT, SKIPPED, UNREADABLE]
    assert claude_health.kimi_counts(entries) == (6, 2, 2)  # skipped: no call; unreadable: a reply, not failed
    assert claude_health.kimi_line(entries) == "Kimi: 6 calls, 2 without search, 2 failed"
    assert claude_health.kimi_counts([]) == (0, 0, 0)


def _files(post, kimi_value, kimi):  # type: ignore[no-untyped-def]
    log = {"question": {"id_of_post": post, "question_type": "binary"}, "final_forecast": 0.6,
           "started_at": "2026-10-30T12:03:09+00:00", "submitted": True}
    shadows = {"shadow": {scoreboard.KIMI_SHADOW_VARIANT: kimi_value}, "kimi_shadow": kimi}
    return {f"questions/tournament/2026-10-30/{post}_120309.json": log,
            f"questions/tournament/2026-10-30/{post}_120309_shadows.json": shadows}


def test_scoreboard_scores_kimi_on_all_answers_and_on_searched_answers_only():
    files = {**_files(1, 0.7, SAVED), **_files(2, 0.2, NO_SEARCH)}
    records = {r["question"]["id_of_post"]: r for r in scoreboard.merge_shadow_files(files)}
    assert records[1]["shadow"][scoreboard.KIMI_SHADOW_SEARCHED] == 0.7
    assert scoreboard.KIMI_SHADOW_SEARCHED not in records[2]["shadow"]  # cited 0 domains: no search
    assert records[2]["shadow"][scoreboard.KIMI_SHADOW_VARIANT] == 0.2  # still in "all answers"
    rows = scoreboard.score_models(list(records.values()), lambda post: "yes", switch_on="2026-10-21T18:41:31+00:00")
    names = [[m for m, _, _ in r["models"]] for r in rows]
    assert names[0] == [scoreboard.KIMI_SHADOW_NAME, scoreboard.KIMI_SHADOW_NAME_NO_METACULUS,
                        scoreboard.KIMI_SHADOW_NAME_SEARCHED]
    assert names[1] == [scoreboard.KIMI_SHADOW_NAME, scoreboard.KIMI_SHADOW_NAME_NO_METACULUS]
    table = scoreboard.per_model_markdown(rows, switch_on="2026-10-21T18:41:31+00:00")
    assert "| binary | moonshotai/kimi-k3 (Kimi shadow, searched only) | other | 1 |" in table
    assert "| binary | moonshotai/kimi-k3 (Kimi shadow) | other | 2 |" in table


def test_scoreboard_counts_the_no_search_answers_over_every_attempt():
    records = scoreboard.merge_shadow_files({**_files(1, 0.7, SAVED), **_files(2, 0.2, NO_SEARCH), **_files(3, None, FAILED)})
    assert scoreboard.kimi_section(records) == (
        "**Kimi shadow, every logged attempt:** Kimi: 3 calls, 1 without search, 1 failed (no search = 0 domains cited)\n")


def test_daily_report_line_and_warning_only_when_every_call_failed():
    ok = health_check.Report()
    claude_health.check_kimi([{"kimi_shadow": SAVED}, {"kimi_shadow": NO_SEARCH}, {"kimi_shadow": FAILED}, {"shadow": {}}], ok)
    assert ok.ok == ["Kimi: 3 calls, 1 without search, 1 failed (last 24 hours)"] and not ok.warnings and not ok.red
    down = health_check.Report()
    claude_health.check_kimi([{"kimi_shadow": FAILED}, {"kimi_shadow": TIMED_OUT}], down)
    assert down.warnings == ["Kimi: 2 calls, 0 without search, 2 failed (last 24 hours): every call failed"]
    none = health_check.Report()
    claude_health.check_kimi([], none)
    assert none.ok == ["Kimi: 0 calls, 0 without search, 0 failed (last 24 hours)"]


def test_shadow_files_of_the_last_24_hours_only(monkeypatch):
    from datetime import datetime, timezone

    tree = {"tree": [{"path": p} for p in (
        "questions/tournament/2026-10-31/46289_150311.json",
        "questions/tournament/2026-10-31/46289_150311_shadows.json",
        "questions/tournament/2026-10-30/46286_120309_shadows.json",  # older than 24 hours
        "questions/test_questions/2026-10-31/1_150311_shadows.json",
    )]}
    asked = []

    def data(path, token):  # type: ignore[no-untyped-def]
        if path.startswith("git/trees"):
            return tree
        asked.append(path)
        import base64
        import json
        return {"content": base64.b64encode(json.dumps({"kimi_shadow": SAVED}).encode()).decode()}

    monkeypatch.setattr(claude_health, "_data", data)
    files = claude_health.recent_shadow_files("t", datetime(2026, 10, 31, 0, 0, tzinfo=timezone.utc))
    assert asked == ["contents/questions/tournament/2026-10-31/46289_150311_shadows.json"]
    assert files == [{"kimi_shadow": SAVED}]
