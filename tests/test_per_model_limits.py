"""Per-model Gemini limits (AI Studio page, 28 Sep 2026): Flash 20/day and
5/minute; Flash-Lite 500/day (we plan with 400) and 15/minute;
3.1-flash-lite-preview shares 3.1-flash-lite's quota. No model calls."""
from __future__ import annotations

import bot_config
import gemini_budget
import health_check
from bot_config import GEMINI_FORECAST_MODELS, GEMINI_PARSER_MODELS
from gemini_budget import MemoryStore, QuotaLedger

LITE_35 = "gemini/gemini-3.5-flash-lite"
LITE_31 = "gemini/gemini-3.1-flash-lite"
PREVIEW = "gemini/gemini-3.1-flash-lite-preview"


def _ledger(used=None, day=None):
    return QuotaLedger(
        MemoryStore({"day": day or gemini_budget.quota_day(), "used": used or {}}),
        daily_limits=dict(bot_config.GEMINI_DAILY_LIMITS),
        reserve_fraction=bot_config.GEMINI_FREE_RESERVE,
        buckets=bot_config.GEMINI_QUOTA_BUCKETS,
    )


def test_limits_per_model():
    limits = bot_config.GEMINI_DAILY_LIMITS
    assert all(limits[m] == 20 for m in GEMINI_FORECAST_MODELS)
    assert limits[LITE_35] == limits[LITE_31] == 400
    assert PREVIEW not in limits  # it has no quota of its own
    ledger = _ledger()
    # The 20% reserve logic is unchanged: 16 usable of 20, 320 of 400.
    assert ledger.usable_left("gemini/gemini-3.6-flash") == 16
    assert ledger.usable_left(LITE_35) == 320 and ledger.total_left(LITE_35) == 400


def test_preview_books_against_3_1_flash_lite():
    ledger = _ledger({LITE_31: 100})
    assert ledger.total_left(PREVIEW) == ledger.total_left(LITE_31) == 300
    assert ledger.start(PREVIEW, booked=False, allow_reserve=False)
    ledger.finish(PREVIEW, succeeded=True)
    assert ledger.used == {LITE_31: 101}
    assert ledger.total_left(LITE_31) == 299
    ledger.book(LITE_31)
    assert ledger.usable_left(PREVIEW) == 320 - 101 - 1
    # A 429 on either one uses up both.
    ledger.mark_used_up(PREVIEW)
    assert ledger.used[LITE_31] == 400 and ledger.total_left(PREVIEW) <= 0
    assert ledger.total_left(LITE_35) == 400  # its own bucket


def test_shared_bucket_reserve_holds_for_both():
    ledger = _ledger({LITE_31: 320})  # at the reserve
    assert not ledger.start(PREVIEW, booked=False, allow_reserve=False)
    assert ledger.start(PREVIEW, booked=False, allow_reserve=True)


def test_old_ledger_with_preview_counts_is_merged():
    ledger = _ledger({LITE_31: 30, PREVIEW: 9, LITE_35: 5})
    assert ledger.used == {LITE_31: 39, LITE_35: 5}
    assert ledger.snapshot()["used"] == {LITE_35: 5, LITE_31: 39}


def test_pacers_per_minute_and_shared():
    pacers = bot_config.gemini_pacers()
    assert all(pacers[m].requests_per_minute == 4 for m in GEMINI_FORECAST_MODELS)  # under 5
    assert pacers[LITE_35].requests_per_minute == pacers[LITE_31].requests_per_minute == 12  # under 15
    assert pacers[PREVIEW] is pacers[LITE_31]


def test_live_lineup_uses_the_new_limits(monkeypatch):
    monkeypatch.delenv("DATA_REPO_TOKEN", raising=False)
    pool = bot_config.get_lineup("gemini-free").planner
    assert pool.ledger.total_left(LITE_35) == 400
    assert pool.ledger.bucket(PREVIEW) == LITE_31
    assert pool.pacers[PREVIEW] is pool.pacers[LITE_31]
    assert set(GEMINI_PARSER_MODELS) <= set(pool.pacers)


def test_health_low_quota_warning_uses_the_new_limits():
    report = health_check.Report()
    used = {"gemini/gemini-3.8-flash": 17, LITE_31: 330, PREVIEW: 20, LITE_35: 100}
    health_check.check_gemini_quota({"day": "2026-09-28", "used": used}, "2026-09-28", report)
    [warning] = report.warnings
    assert "3.8-flash 3/20" in warning
    assert "3.1-flash-lite 50/400" in warning  # 330 + 20 shared, below 20% of 400
    assert "3.5-flash-lite" not in warning  # 300 of 400 left
    assert "preview" not in warning
    ok = health_check.Report()
    # 21 Flash-Lite requests used would have warned under the old 20/day; not now.
    health_check.check_gemini_quota({"day": "2026-09-28", "used": {LITE_35: 21}}, "2026-09-28", ok)
    assert not ok.warnings
