"""Binary round 2 adds 1 model, not 2, once less than half of today's forecast quota is left."""
from __future__ import annotations

from bot_config import GEMINI_FORECAST_MODELS
from tests.test_gemini_budget import make_pool

F36, F37, F38, F35 = GEMINI_FORECAST_MODELS


def test_two_extra_with_plenty_of_quota():
    pool = make_pool()
    assert pool.forecast_quota_left_fraction() == 1.0
    assert len(pool.round2(seasonal=False, used_models=[F36])) == 2


def test_one_extra_below_half_quota():
    # 80 in total; 41 used -> 39 left (< 50%)
    pool = make_pool({F36: 11, F37: 10, F38: 10, F35: 10})
    assert pool.forecast_quota_left_fraction() < 0.5
    extra = pool.round2(seasonal=False, used_models=[F36])
    assert len(extra) == 1


def test_exactly_half_still_two():
    pool = make_pool({F36: 10, F37: 10, F38: 10, F35: 10})  # 40 of 80 left
    assert pool.forecast_quota_left_fraction() == 0.5
    assert len(pool.round2(seasonal=False, used_models=[F36])) == 2
