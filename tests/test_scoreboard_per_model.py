"""Per-model scores, binary subset medians and the before / since frontier
split (scoreboard.py, calibration.py). Fake logs, no network."""
from __future__ import annotations

import math

import pytest

import calibration
import scoreboard
from forecast_safety import adjust_binary

AGENT = scoreboard.AGENT_MODEL
OPUS_55, OPUS_5, HAIKU = "anthropic/claude-opus-5-5", "anthropic/claude-opus-5", "anthropic/claude-haiku-5-5"
FLASH, PAID_FLASH = "gemini/gemini-3.5-flash", "openrouter/google/gemini-3.6-flash"
SWITCH = "2026-10-25T00:00:00+00:00"


def entry(model, parsed, status="ok", started_at=None, old=False):  # type: ignore[no-untyped-def]
    e = {"kind": "planned", "status": status, "parsed": parsed, "answered_models": [model, "parser/model"]}
    if not old:
        e["forecast_model"] = model
    if started_at:
        e["started_at"] = started_at
    return e


def record(post, kind, final, forecasts, started_at="2026-10-22T10:00:00+00:00"):  # type: ignore[no-untyped-def]
    return {"question": {"id_of_post": post, "question_type": kind}, "final_forecast": final,
            "started_at": started_at, "forecasts": forecasts, "submitted": True}


def test_model_names_and_groups():
    assert scoreboard.model_group(AGENT) == "agent"
    assert {scoreboard.model_group(m) for m in (OPUS_55, OPUS_5, HAIKU)} == {"claude"}
    assert scoreboard.model_group(FLASH) == scoreboard.model_group(PAID_FLASH) == "flash"
    assert scoreboard.model_group("gemini/gemini-3.1-flash-lite") == "flash"
    assert scoreboard.model_group("openrouter/nvidia/nemotron:free") == "other"
    assert scoreboard.forecast_model({"forecast_model": f"replay/{AGENT}"}) == AGENT
    # Older logs and reused forecasts: the first model that answered.
    assert scoreboard.forecast_model({"answered_models": [OPUS_5, "parser"]}) == OPUS_5
    assert scoreboard.forecast_model({"answered_models": []}) is None


def test_per_model_scores_and_subset_medians_use_the_live_final_steps():
    forecasts = [entry(AGENT, 0.9), entry(OPUS_55, 0.8), entry(OPUS_5, 0.6, old=True),
                 entry(FLASH, 0.3), entry(PAID_FLASH, 0.4), entry(HAIKU, None, status="failed")]
    rows = scoreboard.score_models([record(1, "binary", 0.55, forecasts)], {1: "yes"}.get)
    [row] = rows
    assert dict((m, s) for m, _, s in row["models"]) == pytest.approx(
        {AGENT: math.log(0.9), OPUS_55: math.log(0.8), OPUS_5: math.log(0.6), FLASH: math.log(0.3), PAID_FLASH: math.log(0.4)})
    assert row["subsets"]["claude-only"] == pytest.approx(math.log(adjust_binary([0.9, 0.8, 0.6])))
    assert row["subsets"]["flash-only"] == pytest.approx(math.log(adjust_binary([0.3, 0.4])))
    assert row["subsets"]["no-agent"] == pytest.approx(math.log(adjust_binary([0.8, 0.6, 0.3, 0.4])))
    report = scoreboard.per_model_markdown(rows)
    assert f"| binary | {AGENT} | agent | 1 | {math.log(0.9):.4f} | {math.log(0.9) - math.log(0.55):+.4f} |" in report
    assert f"| claude-only | 1 | {math.log(0.8):.4f} | {math.log(0.8) - math.log(0.55):+.4f} |" in report
    # Agent first, then Claude, then Flash.
    assert report.index(f"| {AGENT} |") < report.index(f"| {OPUS_5} |") < report.index(f"| {FLASH} |")


def test_subset_goes_through_the_clip_and_extreme_check():
    # Two Flash at 99%: the extreme check keeps it (all agree), the clip makes it 98%.
    rows = scoreboard.score_models([record(1, "binary", 0.9, [entry(FLASH, 0.99), entry(FLASH, 0.99), entry(OPUS_55, 0.5)])],
                                   {1: "no"}.get)
    assert rows[0]["subsets"]["flash-only"] == pytest.approx(math.log(1 - 0.98))
    # No agent here: no-agent = every forecast, through the same steps.
    assert rows[0]["subsets"]["no-agent"] == pytest.approx(math.log(1 - adjust_binary([0.99, 0.99, 0.5])))


def test_models_missing():
    rows = scoreboard.score_models([
        record(1, "binary", 0.6, [entry(FLASH, 0.6)]),  # no Claude, no agent
        record(2, "binary", 0.5, []),  # no forecasts logged
        record(3, "binary", 0.5, [entry(OPUS_55, 0.5)]),  # not resolved
    ], {1: "yes", 2: "no"}.get)
    assert len(rows) == 2
    assert set(rows[0]["subsets"]) == {"flash-only", "no-agent"}
    assert rows[1]["models"] == [] and rows[1]["subsets"] == {}
    report = scoreboard.per_model_markdown(rows)
    assert "claude-only" not in report and "| flash-only | 1 |" in report
    assert "No resolved per-model forecasts." in report  # the "since" part
    assert "not set (everything is before)" in report
    assert "Frontier" in scoreboard.per_model_markdown([])


def test_before_and_since_the_switch_on():
    assert scoreboard.period_of("2026-10-30T00:00:00Z", None) == "before"  # not set yet
    assert scoreboard.period_of("2026-10-24T23:59:59+00:00", SWITCH) == "before"
    assert scoreboard.period_of("2026-10-25T00:00:00Z", SWITCH) == "since"
    assert scoreboard.period_of("2026-10-26T00:00:00", SWITCH) == "since"  # naive = UTC
    assert scoreboard.period_of(None, SWITCH) == "before"
    records = [
        record(1, "binary", 0.7, [entry(FLASH, 0.7)], started_at="2026-10-20T00:00:00+00:00"),
        record(2, "binary", 0.8, [entry(OPUS_55, 0.8)], started_at="2026-10-26T00:00:00+00:00"),
        # A forecast reused from before the switch-on keeps its own time.
        record(3, "binary", 0.6, [entry(FLASH, 0.6, started_at="2026-10-24T00:00:00+00:00"), entry(OPUS_55, 0.6)],
               started_at="2026-10-26T00:00:00+00:00"),
    ]
    rows = scoreboard.score_models(records, {1: "yes", 2: "yes", 3: "yes"}.get, SWITCH)
    assert [r["period"] for r in rows] == ["before", "since", "since"]
    assert rows[2]["models"][0][1] == "before" and rows[2]["models"][1][1] == "since"
    report = scoreboard.per_model_markdown(rows, SWITCH)
    before, since = report.split("**Since frontier switch-on:**")
    assert f"| binary | {FLASH} | flash | 2 |" in before and OPUS_55 not in before
    assert f"| binary | {OPUS_55} | claude | 2 |" in since and FLASH not in since.split("Binary subset")[0]
    assert "| claude-only | 2 |" in since and "| flash-only | 1 |" in before


def test_per_model_multiple_choice_and_numeric():
    mc = lambda a: {"predicted_options": [{"option_name": "A", "probability": a}, {"option_name": "B", "probability": 1 - a}]}  # noqa: E731
    cdf = lambda mid: {"declared_percentiles": [  # noqa: E731
        {"value": 0, "percentile": 0.0}, {"value": 10, "percentile": mid}, {"value": 20, "percentile": 1.0}]}
    rows = scoreboard.score_models([
        record(1, "multiple_choice", mc(0.5), [entry(OPUS_55, mc(0.7)), entry(FLASH, mc(0.2))]),
        record(2, "numeric", cdf(0.5), [entry(OPUS_55, cdf(0.8)), entry(FLASH, cdf(0.1))]),
        record(3, "discrete", cdf(0.5), [entry(OPUS_55, "not a forecast")]),
    ], {1: "A", 2: "5", 3: "15"}.get)
    assert dict((m, s) for m, _, s in rows[0]["models"]) == pytest.approx({OPUS_55: math.log(0.7), FLASH: math.log(0.2)})
    assert dict((m, s) for m, _, s in rows[1]["models"]) == pytest.approx({OPUS_55: math.log(0.8), FLASH: math.log(0.1)})
    assert rows[2]["models"] == [] and rows[0]["subsets"] == {} and rows[1]["subsets"] == {}
    report = scoreboard.per_model_markdown(rows)
    assert f"| multiple_choice | {OPUS_55} | claude | 1 |" in report and f"| numeric | {FLASH} | flash | 1 |" in report


def test_main_keeps_per_model_tables_out_of_the_public_summary(monkeypatch, tmp_path, capsys):
    rec = record(1, "binary", 0.7, [entry(AGENT, 0.9), entry(FLASH, 0.5)])
    saved = {}
    monkeypatch.setenv("DATA_REPO_TOKEN", "test")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setattr(scoreboard, "resolution_lookup", lambda: {1: "yes"}.get)
    monkeypatch.setattr(scoreboard, "all_tournament_records", lambda token: [rec])
    monkeypatch.setattr(scoreboard, "lab_section", lambda token: "")
    monkeypatch.setattr(scoreboard, "save_markdown", lambda path, text, token, message: saved.update({path: text}))
    scoreboard.main()
    public = capsys.readouterr().out + (tmp_path / "summary.md").read_text()
    [text] = saved.values()
    assert AGENT in text and "claude-only" in text and "| agent |" in text
    assert AGENT not in public and "claude-only" not in public and "Per model" not in public


def test_calibration_report_split_and_per_model_section():
    records = [
        {**record(1, "binary", 0.7, [entry(OPUS_55, 0.7)], started_at="2026-10-20T00:00:00+00:00"), "seasonal": True},
        {**record(2, "binary", 0.2, [entry(OPUS_55, 0.2)], started_at="2026-10-26T00:00:00+00:00"), "seasonal": True},
    ]
    report, scored = calibration.build_report(records, {1: "yes", 2: "no"}.get, "2026-10-27", switch_on=SWITCH)
    assert scored == 2
    before = report.split("### seasonal, since frontier switch-on")[0]
    since = report.split("### seasonal, since frontier switch-on")[1].split("### MiniBench")[0]
    assert "### seasonal, before frontier switch-on" in before and "Binary: 1 resolved; resolved Yes 100.0%" in before
    assert "Binary: 1 resolved; resolved Yes 0.0%" in since
    assert "### Per model and binary subsets" in report and f"| binary | {OPUS_55} | claude | 1 |" in report
    # Not set: everything is "before".
    report, _ = calibration.build_report(records, {1: "yes", 2: "no"}.get)
    assert "Binary: 2 resolved" in report.split("### seasonal, since")[0]
