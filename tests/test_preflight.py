"""Credits pre-flight: model ids, reasoning support, prices (fake model list)."""
from __future__ import annotations

import ensemble
import preflight


def _model(model_id, prompt="0.000003", completion="0.000015", reasoning=True):
    return {
        "id": model_id,
        "pricing": {"prompt": prompt, "completion": completion},
        "supported_parameters": ["max_tokens", *(["reasoning"] if reasoning else [])],
        "context_length": 200000,
    }


def test_every_credits_model_is_checked():
    ids = preflight.credits_model_ids()
    assert set(ids) == {ensemble.OPUS_55, ensemble.OPUS_5, ensemble.FABLE_51, ensemble.GPT_SOL,
                        ensemble.GPT_55, ensemble.FLASH_36, ensemble.GEMINI_31_PRO}
    assert len(ids) == len(set(ids))


def test_missing_id_and_prices():
    models = {m["id"]: m for m in [_model("anthropic/claude-opus-5.5"), _model("x/free:free", "0", "0", reasoning=False)]}
    rows, problems = preflight.check(models, ["openrouter/anthropic/claude-opus-5.5", "openrouter/openai/gpt-9"], ["openrouter/x/free:free"])
    opus, gpt, free = rows
    assert opus["exists"] and opus["reasoning"] and opus["prompt_per_m"] == 3.0 and opus["completion_per_m"] == 15.0
    assert not gpt["exists"]
    assert free["prompt_per_m"] == 0 and not free["reasoning"]
    assert problems == ["openai/gpt-9 does not exist on OpenRouter"]
    assert "**NO**" in preflight.table(rows)


def test_a_free_model_with_a_price_is_a_problem():
    models = {"x/free:free": _model("x/free:free")}
    _, problems = preflight.check(models, [], ["openrouter/x/free:free"])
    assert problems == ["x/free:free is not free"]
