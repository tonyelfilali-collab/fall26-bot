"""Replay lab: quota rules (never the live reserve), leak guard, frozen dossier, scores, end to end."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from forecasting_tools import BinaryQuestion

import bot_config
import lab
from gemini_budget import MemoryStore
from replay import ReplayChainLlm

PT = ZoneInfo("America/Los_Angeles")
FLASH = tuple(bot_config.GEMINI_FORECAST_MODELS)
IN_WINDOW = datetime(2026, 10, 6, 22, 30, tzinfo=PT).astimezone(timezone.utc)  # last 3 h (PDT)


def _pool(used=None):
    import gemini_budget

    # The ledger's own quota day (today): a saved day that doesn't match starts from 0.
    store = MemoryStore({"day": gemini_budget.quota_day(), "used": used or {}})
    pool = bot_config._gemini_pool(store, llm_class=ReplayChainLlm)
    assert pool.ledger.used == (used or {})
    return pool


# ---------------------------------------------------------------- quota rules


@pytest.mark.parametrize(
    "local, allowed",
    [
        (datetime(2026, 10, 6, 20, 59, tzinfo=PT), False),
        (datetime(2026, 10, 6, 21, 0, tzinfo=PT), True),
        (datetime(2026, 10, 6, 23, 59, tzinfo=PT), True),
        (datetime(2026, 11, 3, 21, 30, tzinfo=PT), True),  # winter time too (08:00 UTC day change)
        (datetime(2026, 11, 3, 12, 0, tzinfo=PT), False),
    ],
)
def test_gemini_only_in_the_last_3_hours_of_the_pacific_day(local, allowed):
    quota = lab.LabQuota(local.astimezone(timezone.utc), None, 0.0)
    assert (quota.gemini_window() is None) is allowed


def test_never_while_minibench_open_or_in_the_last_6_hours():
    assert lab.LabQuota(IN_WINDOW, IN_WINDOW - timedelta(hours=5, minutes=59), 0.0).gemini_window() is not None
    assert lab.LabQuota(IN_WINDOW, IN_WINDOW - timedelta(hours=6, minutes=1), 0.0).gemini_window() is None


def test_only_non_reserve_quota_beyond_2x_expected():
    pool = _pool()  # 4 x 16 usable
    quota = lab.LabQuota(IN_WINDOW, None, expected_questions_left=10.0)
    assert quota.gemini_calls_left(pool.ledger, FLASH) == 64 - 20
    at_reserve = _pool({m: 16 for m in FLASH})  # usable 0: only the reserve is left
    assert quota.gemini_calls_left(at_reserve.ledger, FLASH) == 0
    assert lab.gemini_model_for_lab(at_reserve.ledger, quota, FLASH, set()) is None
    busy_day = lab.LabQuota(IN_WINDOW, None, expected_questions_left=40.0)  # 2 x 40 > 64
    assert busy_day.gemini_calls_left(pool.ledger, FLASH) == 0


def test_nemotron_300_a_day():
    quota = lab.LabQuota(IN_WINDOW, None, 0.0, nemotron_used_today=299)
    assert quota.nemotron_calls_left() == 1
    quota.nemotron_started = 1
    assert quota.nemotron_calls_left() == 0


def _record(post=5, forecasts=3, sections=True, opened=datetime(2026, 10, 1, tzinfo=timezone.utc)):
    q = BinaryQuestion(question_text="Will X?", id_of_post=post, page_url=f"https://www.metaculus.com/questions/{post}", open_time=opened)
    record = {"question": q.model_dump(mode="json", exclude={"api_json"}), "final_forecast": 0.4,
              "forecasts": [{"kind": "planned", "status": "ok", "parsed": 0.4} for _ in range(forecasts)]}
    if sections:
        record["dossier_sections"] = {"base": {"text": "Base."}, "followup": {"text": "Follow-up."}, "wikipedia": {"text": "## Background (Wikipedia)\nW."}}
    return record


def test_the_lab_never_touches_the_live_reserve_through_the_real_ledger():
    # Real ledger and real ThrottledLlm chains (recorded replies): quota nearly
    # gone, so the lab may use only what's beyond 2 x expected; the reserve holds.
    import replay

    used = {m: 14 for m in FLASH}  # 2 usable each = 8 usable, reserve 4 each
    pool = _pool(used)
    quota = lab.LabQuota(IN_WINDOW, None, expected_questions_left=2.0)  # may use 8 - 4 = 4
    forecast = lab.forecast_with_model(lab._bot_factory(pool, bot_config.BACKUP_FORECAST_MODEL))
    release = {m: datetime(2026, 9, 1, tzinfo=timezone.utc) for m in FLASH}  # Nemotron: unknown -> not used
    ctx = lab.LabContext(quota, pool.ledger, release, forecast, FLASH, bot_config.BACKUP_FORECAST_MODEL)
    replay._RecordedAnswer.failing_model = None
    results = lab.run_all(ctx, [_record(post=p) for p in (5, 6, 7)], lambda post: "yes")
    for m in FLASH:
        assert pool.ledger.used[m] <= 16  # never past the usable part: the reserve is untouched
        assert pool.ledger.total_left(m) >= 4
    spent = sum(pool.ledger.used[m] - 14 for m in FLASH)
    assert 0 < spent <= 4  # it did run, and only within 8 usable - 2 x 2 expected
    assert any("pending" in r.note for r in results)  # the rest waits for another day
    assert all(r.experiment != lab.EXPERIMENTS[3] or "Nemotron not allowed" in r.note for r in results)


# ---------------------------------------------------------------- leak guard


def test_leak_guard():
    opened = datetime(2026, 10, 1, tzinfo=timezone.utc)
    dates = {"a": datetime(2026, 9, 1, tzinfo=timezone.utc), "b": datetime(2026, 10, 2, tzinfo=timezone.utc)}
    assert lab.leak_safe("a", opened, dates)
    assert not lab.leak_safe("b", opened, dates)  # released after the question opened
    assert not lab.leak_safe("c", opened, dates)  # unknown release date


def test_models_released_after_the_question_opened_are_skipped():
    calls = []

    def forecast(q, research, model):
        calls.append(model)
        return 0.5

    release = {m: datetime(2026, 10, 5, tzinfo=timezone.utc) for m in FLASH}  # all newer than the question
    ctx = lab.LabContext(lab.LabQuota(IN_WINDOW, None, 0.0), _pool().ledger, release, forecast, FLASH, bot_config.BACKUP_FORECAST_MODEL)
    results = lab.run_question(ctx, _record(), "yes")
    assert calls == [] and all(r.diff is None for r in results)


# ---------------------------------------------------------------- dossier, scores, report


def test_frozen_dossier_drops_a_section():
    sections = {"base": {"text": "BASE"}, "followup": {"text": "FOLLOW"}, "wikipedia": {"text": "WIKI"}, "official_data": {"text": "OFFICIAL"}}
    full = lab.frozen_dossier(sections)
    assert full.startswith("OFFICIAL") and "WIKI" in full and "FOLLOW" in full
    assert "FOLLOW" not in lab.frozen_dossier(sections, drop=("followup",))
    assert "WIKI" not in lab.frozen_dossier(sections, drop=("wikipedia",))
    assert lab.frozen_dossier({"followup": {"text": "x"}}) is None


def test_numeric_log_density():
    forecast = {"declared_percentiles": [{"value": 0.0, "percentile": 0.1}, {"value": 50.0, "percentile": 0.6}, {"value": 100.0, "percentile": 0.9}]}
    # 0.5 mass over half the range -> density 1.0 -> log 0
    assert lab.numeric_log_density(forecast, "25", 0.0, 100.0) == pytest.approx(0.0)
    assert lab.numeric_log_density(forecast, "below_lower_bound", 0.0, 100.0) == pytest.approx(-2.302585, rel=1e-5)


def test_e1_is_free_when_live_had_5():
    calls = []
    ctx = lab.LabContext(lab.LabQuota(IN_WINDOW, None, 0.0), _pool().ledger, {}, lambda *a: calls.append(a), FLASH, bot_config.BACKUP_FORECAST_MODEL)
    [e1] = [r for r in lab.run_question(ctx, _record(forecasts=5), "yes") if r.experiment.startswith("E1")]
    assert e1.diff == pytest.approx(0.0) and "first 3" in e1.note and calls == []


def test_synthetic_end_to_end_report():
    text = lab.synthetic_run()
    for experiment in lab.EXPERIMENTS:
        assert experiment in text
    assert "nothing submitted" in text
    assert "90% CI" in text


def test_lab_never_submits():
    source = open("lab.py").read()
    assert "publish_report_to_metaculus" not in source and "submit_forecasts = True" not in source


def test_live_records_keep_the_dossier_sections():
    import asyncio
    import json
    from types import SimpleNamespace

    import main
    import research

    async def helper(prompt):
        if "plan news research" in prompt:
            return json.dumps({"queries": ["q"], "key_facts": [], "entities": ["X"]})
        return "## Status\nThe base research.\nMISSING: none"

    async def asknews(query):
        return []

    q = SimpleNamespace(question_text="Will X?", resolution_criteria="", id_of_post=1, question_type="binary")
    result = asyncio.run(research.run_planned_research(q, "Today", helper, asknews, lambda query: [],
                                                       lambda entities: ("## Background (Wikipedia, retrieved 2026-10-01)\nX is.", ["X"])))
    assert "base research" in result.base_dossier and result.wikipedia_section.startswith("## Background")
    bot = main.FallBot2026(llms={"default": "openrouter/x:free", "parser": "openrouter/x:free", "summarizer": "openrouter/x:free", "researcher": "no_research"},
                           publish_reports_to_metaculus=False)
    question = BinaryQuestion(question_text="Will X?", id_of_post=1, page_url="https://www.metaculus.com/questions/1")
    bot._save_section(question, "followup", "Findings.")
    bot._save_section(question, "wikipedia", "")  # nothing to save
    sections = bot._record_for(question)["dossier_sections"]
    assert set(sections) == {"followup"} and sections["followup"]["text"] == "Findings." and sections["followup"]["at"]


# ---------------------------------------------------------------- time limit and own queue


def test_time_check_leaves_room_for_the_call_and_the_save():
    now = [0.0]
    may_start = lab.time_check(0.0, clock=lambda: now[0])
    usable = (lab.LAB_LIMIT - lab.SAVE_MARGIN).total_seconds()
    assert may_start(lab.NEMOTRON_TIMEOUT)
    now[0] = usable - lab.NEMOTRON_TIMEOUT
    assert may_start(lab.NEMOTRON_TIMEOUT)
    now[0] += 1
    assert not may_start(lab.NEMOTRON_TIMEOUT) and may_start(lab.LOOKUP_SECONDS)
    # The job ends by minute 8: ~1 minute of setup plus the lab's own limit.
    assert lab.LAB_LIMIT + timedelta(minutes=1) <= timedelta(minutes=8)


def test_no_nemotron_call_starts_when_it_could_not_finish_in_time():
    calls = []
    release = {bot_config.BACKUP_FORECAST_MODEL: datetime(2026, 9, 1, tzinfo=timezone.utc)}
    quota = lab.LabQuota(IN_WINDOW, None, 0.0, gemini_off="no Gemini")
    ctx = lab.LabContext(quota, None, release, lambda *a: calls.append(a) or 0.5, FLASH,
                         bot_config.BACKUP_FORECAST_MODEL, may_start=lambda seconds: False)
    [e4] = [r for r in lab.run_question(ctx, _record(), "yes") if r.experiment == lab.EXPERIMENTS[3]]
    assert calls == [] and quota.nemotron_started == 0 and "time limit" in e4.note


def test_without_gemini_only_e4_runs_and_the_ledger_is_never_touched():
    calls = []
    release = {bot_config.BACKUP_FORECAST_MODEL: datetime(2026, 9, 1, tzinfo=timezone.utc), **{m: datetime(2026, 9, 1, tzinfo=timezone.utc) for m in FLASH}}
    quota = lab.LabQuota(IN_WINDOW, None, 0.0, gemini_off="no Gemini")
    ctx = lab.LabContext(quota, None, release, lambda q, r, m: calls.append(m) or 0.5, FLASH, bot_config.BACKUP_FORECAST_MODEL)
    results = lab.run_question(ctx, _record(), "yes")  # ledger None: any Gemini use would fail
    assert calls == [bot_config.BACKUP_FORECAST_MODEL] and quota.gemini_started == 0
    assert {r.experiment for r in results if r.diff is not None} == {lab.EXPERIMENTS[3]}


def test_lab_records_loads_only_what_it_needs_and_stops_in_time():
    paths = ["questions/tournament/2026-10-01/5_100000.json", "questions/tournament/2026-10-02/5_090000.json",
             "questions/tournament/2026-10-02/5_090000_shadows.json", "questions/tournament/2026-10-01/6_100000.json",
             "questions/tournament/2026-10-01/7_100000.json", "questions/test_questions/2026-10-01/8_100000.json"]
    files = {"questions/tournament/2026-10-02/5_090000.json": {"submitted": False, "id": "new-unsubmitted"},
             "questions/tournament/2026-10-01/5_100000.json": {"submitted": True, "id": "5"},
             "questions/tournament/2026-10-01/7_100000.json": {"submitted": True, "id": "7"}}
    loaded, looked_up = [], []

    def load(path):
        loaded.append(path)
        return files[path]

    def resolution_of(post):
        looked_up.append(post)
        return {5: "yes", 6: None, 7: "no"}[post]

    pairs = lab.lab_records(paths, load, resolution_of, skip_post=lambda post: False, may_start=lambda s: True)
    assert [(r["id"], res) for r, res in pairs] == [("5", "yes"), ("7", "no")]
    assert looked_up == [5, 6, 7] and not any("shadows" in p or "test_questions" in p for p in loaded)
    loaded.clear(), looked_up.clear()
    assert lab.lab_records(paths, load, resolution_of, skip_post=lambda post: post == 5, may_start=lambda s: True)[0][0]["id"] == "7"
    assert 5 not in looked_up
    assert lab.lab_records(paths, load, resolution_of, skip_post=lambda post: False, may_start=lambda s: False) == []


def test_the_lab_workflow_never_uses_the_live_queue_or_gemini():
    import yaml

    workflow = yaml.safe_load(open(".github/workflows/replay_lab.yaml"))
    assert workflow["concurrency"]["group"] != "forecast-bot"
    assert workflow["jobs"]["lab"]["timeout-minutes"] <= 9
    assert "GEMINI_API_KEY" not in open(".github/workflows/replay_lab.yaml").read()
