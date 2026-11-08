"""
Aggregation mode (aggregation.py; architect, 8 Nov; Tier C, built OFF).

- "all" (the default, live): every forecast is combined.
- "claude_first": 2+ Claude Opus forecasts -> only those are combined (Flash
  ignored); fewer -> everything, as now. Every type.
- The free shadow variants still use every forecast; the private log notes the choice.
- The switch-on rule (Scoreboard): counts, the two score conditions, the public line.
"""
from __future__ import annotations

import asyncio

import pytest
from forecasting_tools import (
    BinaryQuestion,
    DiscreteQuestion,
    MultipleChoiceQuestion,
    NumericQuestion,
    PredictedOption,
    PredictedOptionList,
)

import aggregation
import bot_config
import main
from distributions import combine_numeric, median_multiple_choice, pchip_distribution
from forecast_safety import adjust_binary
from tests.test_distributions import nine

AGENT = "anthropic/claude-opus-5-5-agent"
AGENT2 = "anthropic/claude-opus-5-5-agent2"
OPUS = "anthropic/claude-opus-5-5"
OPUS_5 = "anthropic/claude-opus-5"
HAIKU = "anthropic/claude-haiku-5-5"
FLASH = "openrouter/google/gemini-3.6-flash"
FLASH_FREE = "gemini/gemini-3.7-flash"


def _bot(mode: str | None = None) -> main.FallBot2026:
    bot = main.FallBot2026(
        llms={"default": "openrouter/x:free", "parser": "openrouter/x:free", "summarizer": "openrouter/x:free",
              "researcher": "no_research"},
        publish_reports_to_metaculus=False,
    )
    bot.aggregation_mode = mode
    return bot


def _run(bot: main.FallBot2026, question, tagged: list[tuple[str, object]]):  # type: ignore[no-untyped-def]
    """Tags each forecast with its model (as forecast_with does) and combines them."""
    values = [v for _, v in tagged]
    for model, value in tagged:
        bot._tag_model(value, model)
    result = asyncio.run(bot._aggregate_predictions(values, question))
    return result, bot._record_for(question)


def _binary() -> BinaryQuestion:
    return BinaryQuestion(question_text="Will it?", id_of_post=11)


def _mc() -> MultipleChoiceQuestion:
    return MultipleChoiceQuestion(question_text="Which?", id_of_post=12, options=["O0", "O1", "O2"])


def _numeric(cls=NumericQuestion, **extra):  # type: ignore[no-untyped-def]
    fields = dict(question_text="How many?", id_of_post=13, unit_of_measure="units", lower_bound=0.0,
                  upper_bound=1000.0, open_lower_bound=False, open_upper_bound=False, zero_point=None, cdf_size=201)
    fields.update(extra)
    return cls(**fields)


def _discrete() -> DiscreteQuestion:
    return _numeric(DiscreteQuestion, id_of_post=14, upper_bound=10.0, cdf_size=11)


def _options(*p: float) -> PredictedOptionList:
    return PredictedOptionList(predicted_options=[PredictedOption(option_name=f"O{i}", probability=x) for i, x in enumerate(p)])


def _flash_low(q):  # type: ignore[no-untyped-def]
    if isinstance(q, DiscreteQuestion):
        return pchip_distribution(nine(0.5, 0.8, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0), q)
    return pchip_distribution(nine(20, 40, 60, 100, 150, 220, 300, 350, 400), q)


def _claude_high(q, shift: float = 0.0):  # type: ignore[no-untyped-def]
    if isinstance(q, DiscreteQuestion):
        return pchip_distribution(nine(*(v + shift for v in (5.0, 5.5, 6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0))), q)
    return pchip_distribution(nine(*(v + shift for v in (500, 550, 600, 650, 700, 750, 800, 850, 900))), q)


def _same_cdf(a, b) -> bool:  # type: ignore[no-untyped-def]
    return [round(p.percentile, 9) for p in a.get_cdf()] == [round(p.percentile, 9) for p in b.get_cdf()]


# ---------------------------------------------------------------- the switch


def test_built_off():
    assert bot_config.AGGREGATION_MODE == "all"
    assert aggregation.MODES == ("all", "claude_first")


def test_which_models_count_as_claude():
    for model in (AGENT, AGENT2, OPUS, OPUS_5, "replay/" + OPUS, "openrouter/anthropic/claude-opus-5.5"):
        assert aggregation.is_claude_forecast(model)
    for model in (HAIKU, FLASH, FLASH_FREE, "openrouter/moonshotai/kimi-k3", None, ""):
        assert not aggregation.is_claude_forecast(model)


def test_choose():
    models = [AGENT, FLASH, OPUS, FLASH_FREE]
    assert aggregation.choose(models, "all") == ([0, 1, 2, 3], "all")
    assert aggregation.choose(models, "claude_first") == ([0, 2], "claude")
    assert aggregation.choose([AGENT, FLASH, FLASH_FREE], "claude_first") == ([0, 1, 2], "all")
    assert aggregation.choose([AGENT, HAIKU, FLASH], "claude_first") == ([0, 1, 2], "all")  # Haiku is not counted
    with pytest.raises(ValueError):
        aggregation.choose(models, "flash_first")


# ---------------------------------------------------------------- live combine, every type


def test_binary_two_claude_two_flash_ignores_flash():
    result, record = _run(_bot("claude_first"), _binary(), [(AGENT, 0.70), (FLASH, 0.10), (AGENT2, 0.60), (FLASH_FREE, 0.12)])
    assert result == pytest.approx(adjust_binary([0.70, 0.60]))
    assert record["aggregation"] == {"mode": "claude_first", "used": "claude", "claude_forecasts": 2, "forecasts": 4}
    # The shadow variants still see every forecast (their "mean" includes Flash).
    assert record["shadow"]["mean"] == pytest.approx((0.70 + 0.10 + 0.60 + 0.12) / 4)


def test_binary_one_claude_two_flash_uses_all():
    result, record = _run(_bot("claude_first"), _binary(), [(OPUS, 0.70), (FLASH, 0.10), (FLASH_FREE, 0.12)])
    assert result == pytest.approx(adjust_binary([0.70, 0.10, 0.12]))
    assert record["aggregation"]["used"] == "all"


def test_binary_default_mode_all_uses_every_forecast():
    result, record = _run(_bot(), _binary(), [(AGENT, 0.70), (FLASH, 0.10), (AGENT2, 0.60), (FLASH_FREE, 0.12)])
    assert result == pytest.approx(adjust_binary([0.70, 0.10, 0.60, 0.12]))
    assert record["aggregation"] == {"mode": "all", "used": "all", "claude_forecasts": 2, "forecasts": 4}


def test_binary_agent_and_plain_opus_count_as_two():
    result, record = _run(_bot("claude_first"), _binary(), [(AGENT, 0.80), (OPUS, 0.60), (FLASH, 0.05), (FLASH, 0.07)])
    assert result == pytest.approx(adjust_binary([0.80, 0.60]))
    assert record["aggregation"]["used"] == "claude"


def test_multiple_choice_two_claude_two_flash_ignores_flash():
    claude = [_options(0.7, 0.2, 0.1), _options(0.6, 0.3, 0.1)]
    flash = [_options(0.1, 0.1, 0.8), _options(0.1, 0.2, 0.7)]
    result, record = _run(_bot("claude_first"), _mc(), [(AGENT, claude[0]), (FLASH, flash[0]), (OPUS, claude[1]), (FLASH, flash[1])])
    expected = median_multiple_choice(claude)
    assert [o.probability for o in result.predicted_options] == pytest.approx([o.probability for o in expected.predicted_options])
    assert record["aggregation"]["used"] == "claude"


def test_multiple_choice_one_claude_uses_all():
    tagged = [(AGENT, _options(0.7, 0.2, 0.1)), (FLASH, _options(0.1, 0.1, 0.8)), (FLASH, _options(0.1, 0.2, 0.7))]
    result, record = _run(_bot("claude_first"), _mc(), tagged)
    expected = median_multiple_choice([v for _, v in tagged])
    assert [o.probability for o in result.predicted_options] == pytest.approx([o.probability for o in expected.predicted_options])
    assert record["aggregation"]["used"] == "all"


@pytest.mark.parametrize("make", [_numeric, _discrete], ids=["numeric", "discrete"])
def test_numeric_two_claude_two_flash_ignores_flash(make):  # type: ignore[no-untyped-def]
    q = make()
    claude = [_claude_high(q), _claude_high(q, shift=0.2 if isinstance(q, DiscreteQuestion) else 20)]
    flash = [_flash_low(q), _flash_low(q)]
    result, record = _run(_bot("claude_first"), q, [(AGENT, claude[0]), (FLASH, flash[0]), (AGENT2, claude[1]), (FLASH_FREE, flash[1])])
    assert _same_cdf(result, combine_numeric(claude, q))
    assert not _same_cdf(result, combine_numeric([*claude, *flash], q))
    assert record["aggregation"]["used"] == "claude"


@pytest.mark.parametrize("make", [_numeric, _discrete], ids=["numeric", "discrete"])
def test_numeric_one_claude_two_flash_uses_all(make):  # type: ignore[no-untyped-def]
    q = make()
    tagged = [(OPUS, _claude_high(q)), (FLASH, _flash_low(q)), (FLASH_FREE, _flash_low(q))]
    result, record = _run(_bot("claude_first"), q, tagged)
    assert _same_cdf(result, combine_numeric([v for _, v in tagged], q))
    assert record["aggregation"]["used"] == "all"


def test_untagged_forecasts_count_as_not_claude():
    # The library's own path (replay lineup) tags nothing: everything is combined.
    bot, q = _bot("claude_first"), _binary()
    result = asyncio.run(bot._aggregate_predictions([0.7, 0.1, 0.6], q))
    assert result == pytest.approx(adjust_binary([0.7, 0.1, 0.6]))
    assert bot._record_for(q)["aggregation"]["used"] == "all"


def test_claude_only_combine_failing_falls_back_to_all(monkeypatch):
    bot = _bot("claude_first")
    calls = []
    real = main.adjust_binary

    def fails_first(values):  # type: ignore[no-untyped-def]
        calls.append(list(values))
        if len(calls) == 1:
            raise main.NoValidForecast("rehearsed failure")
        return real(values)

    monkeypatch.setattr(main, "adjust_binary", fails_first)
    q = _binary()
    result, record = _run(bot, q, [(AGENT, 0.7), (FLASH, 0.1), (OPUS, 0.6)])
    assert calls == [[0.7, 0.6], [0.7, 0.1, 0.6]]
    assert result == pytest.approx(real([0.7, 0.1, 0.6]))
    assert record["aggregation"]["used"] == "all (Claude-only combine failed)"


# ---------------------------------------------------------------- the switch-on rule (Scoreboard)


def _record(post: int, kind: str, final, forecasts: list[tuple[str, object]], started: str = "2026-10-25T12:00:00+00:00") -> dict:  # type: ignore[no-untyped-def]
    return {"question": {"id_of_post": post, "question_type": kind}, "started_at": started, "submitted": True,
            "final_forecast": final,
            "forecasts": [{"kind": "planned", "status": "ok", "forecast_model": m, "parsed": v} for m, v in forecasts]}


SWITCH_ON = "2026-10-21T18:41:31+00:00"


def _median_combine(snapshot, values):  # type: ignore[no-untyped-def]
    import statistics

    return statistics.median(values)


def test_rule_rows_simulate_claude_first():
    records = [
        _record(1, "binary", 0.40, [(AGENT, 0.8), (OPUS, 0.6), (FLASH, 0.1), (FLASH, 0.1)]),  # Claude-only -> 0.7
        _record(2, "binary", 0.30, [(AGENT, 0.8), (FLASH, 0.1), (FLASH, 0.1)]),  # 1 Claude -> submitted
        _record(3, "binary", 0.50, [(AGENT, 0.8), (OPUS, 0.6)], started="2026-10-10T00:00:00+00:00"),  # before: left out
        _record(4, "binary", 0.50, [(AGENT, 0.8), (OPUS, 0.6)]),  # not resolved: left out
    ]
    resolutions = {1: "yes", 2: "no", 3: "yes"}
    rows = aggregation.rule_rows(records, resolutions.get, SWITCH_ON, combine=_median_combine)
    assert [(r["post"], r["used"], r["claude"]) for r in rows] == [(1, "claude", 2), (2, "all", 1)]
    import math

    assert rows[0]["submitted"] == pytest.approx(math.log(0.40)) and rows[0]["claude_first"] == pytest.approx(math.log(0.7))
    assert rows[1]["claude_first"] == rows[1]["submitted"]


def _rows(n: int, gain: float, binary_gain: float, binary: int = 5) -> list[dict]:
    return ([{"type": "binary", "submitted": -0.5, "claude_first": -0.5 + binary_gain, "used": "claude"}] * binary
            + [{"type": "numeric", "submitted": -3.0, "claude_first": -3.0 + (gain * n - binary_gain * binary) / (n - binary),
                "used": "claude"}] * (n - binary))


def test_rule_needs_all_three_conditions():
    assert aggregation.rule_result(_rows(25, 0.15, 0.0))["met"]
    assert not aggregation.rule_result(_rows(24, 0.15, 0.0))["met"]  # too few questions
    assert not aggregation.rule_result(_rows(25, 0.0999, 0.0))["met"]  # gain must be MORE than 0.10
    assert not aggregation.rule_result(_rows(25, 0.15, -0.06))["met"]  # binary worse than -0.05
    assert aggregation.rule_result(_rows(25, 0.15, -0.0499))["met"]  # just above -0.05 is allowed
    result = aggregation.rule_result(_rows(30, 0.2, 0.01))
    assert result["questions"] == 30 and result["binary_questions"] == 5 and result["gain_all"] == pytest.approx(0.2)


def test_health_line_is_public_safe():
    status = {**aggregation.rule_result(_rows(9, 0.13, -0.03, binary=4)), "checked_at": "2026-11-08T12:00:00+00:00"}
    line, warn = aggregation.health_line(status, "all")
    assert not warn and line.startswith("claude_first rule not met (9 of 25 agent-era questions")
    assert "0.13" not in line and "-0.03" not in line  # scores stay in fall26-data
    met = {**aggregation.rule_result(_rows(25, 0.15, 0.0)), "checked_at": "2026-11-20"}
    line, warn = aggregation.health_line(met, "all")
    assert warn and "MET" in line and "pre-approved" in line
    assert aggregation.health_line(met, "claude_first") == ("Aggregation mode: claude_first (switched on)", False)
    assert aggregation.health_line(None, "all")[1] is False


def test_health_check_reports_the_rule():
    import health_check

    report = health_check.Report()
    health_check.check_claude_first(None, "all", report)
    met = {**aggregation.rule_result(_rows(25, 0.15, 0.0)), "checked_at": "2026-11-20"}
    health_check.check_claude_first(met, "all", report)
    assert report.ok == ["claude_first rule: not checked yet (the Scoreboard writes it)"]
    assert len(report.warnings) == 1 and "MET" in report.warnings[0]
