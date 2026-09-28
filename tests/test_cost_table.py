"""Credits 4b: the real cost table (fake logs and prices; no calls)."""
from __future__ import annotations

import pytest
from forecasting_tools import BinaryQuestion, NumericQuestion

import cost_table
import ensemble
from replay import REPLAY_RESEARCH


def _binary(post=1):
    return BinaryQuestion(
        question_text="Will X happen?", background_info="Background.", resolution_criteria="Criteria.",
        fine_print="Fine print.", id_of_post=post, page_url=f"https://www.metaculus.com/questions/{post}",
    )


def _numeric(post=2):
    return NumericQuestion(
        question_text="How many?", id_of_post=post, page_url=f"https://www.metaculus.com/questions/{post}",
        unit_of_measure="things", lower_bound=0.0, upper_bound=100.0, open_lower_bound=False,
        open_upper_bound=False, zero_point=None, cdf_size=201,
    )


def _record(question, research, started):
    return {"question": question.model_dump(mode="json", exclude={"api_json"}), "research": {"text": research}, "started_at": started}


def test_prompt_is_the_bots_own_with_the_dossier():
    prompt = cost_table.forecast_prompt(_binary(), "DOSSIER-TEXT " * 50)
    assert "DOSSIER-TEXT" in prompt and 'Probability: ZZ%' in prompt and "Will X happen?" in prompt


def test_tokens_measured_per_type_latest_log_only():
    long = "word " * 3000
    records = [
        _record(_binary(1), "short " * 100, "2026-09-28T10:00:00"),
        _record(_binary(1), long, "2026-09-28T12:00:00"),  # latest for question 1
        _record(_binary(3), REPLAY_RESEARCH + " " * 500, "2026-09-28T12:00:00"),  # frozen: skipped
        _record(_numeric(2), long, "2026-09-28T12:00:00"),
    ]
    tokens = cost_table.prompt_tokens_from_records(records)
    assert len(tokens["binary"]) == 1 and len(tokens["numeric"]) == 1
    assert tokens["binary"][0] > 3000  # the dossier plus the prompt around it
    assert tokens["discrete"] == [] and tokens["multiple_choice"] == []


def test_cost_math():
    prices = {m.removeprefix("openrouter/"): (1.0, 10.0) for m in [ensemble.OPUS_55, ensemble.GPT_SOL, ensemble.FLASH_36, ensemble.FABLE_51]}
    # One forecast: 2,000 prompt tokens x $1/M + 8,000 x $10/M = $0.082
    assert cost_table.forecast_cost(ensemble.OPUS_55, 2000, prices) == pytest.approx(0.082)
    table = cost_table.cost_table({t: 2000 for t in cost_table.TYPES}, prices)
    assert table["lean"]["binary"] == pytest.approx(2 * 0.082)
    assert table["lean"]["binary+r2"] == pytest.approx(3 * 0.082)
    assert table["full"]["numeric"] == pytest.approx(6 * 0.082)
    assert "ASSUMED" in cost_table.render({t: [2000] for t in cost_table.TYPES}, {t: 2000 for t in cost_table.TYPES}, table)
