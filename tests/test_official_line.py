"""Build 2c, partial: one official-data line in the dossier; only an exact
match's latest value feeds the unit check (#34). No real API calls."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest
from forecasting_tools import NumericQuestion, Percentile

import hard_data
import main
import research
from distributions import STEP8_PERCENTILES


def nine(*values):
    return [Percentile(percentile=h, value=v) for h, v in zip(STEP8_PERCENTILES, values)]


RIGHT = nine(3.5, 3.7, 3.9, 4.1, 4.2, 4.3, 4.5, 4.7, 4.9)  # percent
X11 = nine(*(p.value * 11 for p in RIGHT))  # median 46.2: over 10x the latest 4.2

HISTORY = [["2025-08-01", 5.0], ["2025-10-01", 3.6], ["2026-03-02", 4.9], ["2026-08-26", 4.0], ["2026-09-01", 4.1], ["2026-09-25", 4.2]]


def fred(series="DGS10", title="10-Year Treasury Constant Maturity Rate", history=HISTORY):
    return {
        "source": "fred", "series": series, "title": title, "rule": "x",
        "latest": {"date": history[-1][0], "value": history[-1][1]}, "history": history,
    }


def numeric(text, criteria="", fine_print="", lower=0.0, upper=60.0):
    return NumericQuestion(
        question_text=text, resolution_criteria=criteria, fine_print=fine_print, id_of_post=31,
        page_url="https://www.metaculus.com/questions/31", unit_of_measure="%",
        lower_bound=lower, upper_bound=upper, open_lower_bound=False, open_upper_bound=True,
        zero_point=None, cdf_size=201,
    )


EXACT_Q = numeric("What will the 10-year Treasury yield be on Dec 31, 2026?", "Resolves to FRED series DGS10 on that date.")
VIX_Q = numeric("What will be the maximum intraday value of the VIX in Q4 2026?", "Per Cboe.", lower=10, upper=70)


# ---------------------------------------------------------------- the line


def test_line_has_latest_min_max_and_30_day_change():
    line = hard_data.official_line(EXACT_Q, fred())
    assert line.exact and line.warning is None and line.latest == 4.2
    assert "FRED DGS10" in line.text and "10-Year Treasury Constant Maturity Rate" in line.text
    assert "latest 4.2 on 2026-09-25" in line.text
    # Past year only (from 2025-09-25): the 5.0 of Aug 2025 is older.
    assert "past-year min 3.6, max 4.9" in line.text
    assert "30-day change +0.2 (from 4 on 2026-08-26)" in line.text
    assert "WARNING" not in line.text
    assert "\n" not in line.text  # ONE line


def test_vix_intraday_maximum_is_a_stand_in_with_a_warning():
    line = hard_data.official_line(VIX_Q, fred("VIXCLS", "CBOE Volatility Index: VIX (daily close)", [["2026-08-25", 15.0], ["2026-09-25", 16.5]]))
    assert not line.exact
    assert "WARNING: stand-in: CBOE Volatility Index: VIX (daily close); the question asks about an intraday maximum" in line.text


@pytest.mark.parametrize(
    "question, found, exact",
    [
        # Another site named as the source, ours not: stand-in.
        (numeric("What will the 10-year Treasury yield be?", "According to Yahoo Finance."), fred(), False),
        # FRED named too: exact.
        (numeric("What will the 10-year Treasury yield be?", "Per FRED (DGS10), or Yahoo Finance if FRED is down."), fred(), True),
        # The official publisher named: exact.
        (numeric("What will the 10-year Treasury yield be?", "Per treasury.gov daily par yield curve rates."), fred(), True),
        # An average, not the series' value: stand-in.
        (numeric("What will the average 10-year Treasury yield be in November?", "Per FRED."), fred(), False),
        # Latest value outside the question's range (e.g. basis points): stand-in.
        (numeric("What will the 10-year Treasury yield be, in basis points?", "Per FRED.", lower=300, upper=600), fred(), False),
        # "High yield" is a name, not a maximum.
        (
            numeric("What will the ICE BofA US High Yield Option-Adjusted Spread be?", "Per FRED."),
            fred("BAMLH0A0HYM2", "ICE BofA US High Yield Index Option-Adjusted Spread"),
            True,
        ),
        # CoinGecko is an aggregator: exact only when the question names it.
        (
            numeric("What will the price of Bitcoin be?", "Coinbase BTC-USD close.", lower=0, upper=500000),
            {**fred("bitcoin", "bitcoin price (USD)", [["2026-08-25", 60000.0], ["2026-09-25", 61000.0]]), "source": "coingecko"},
            False,
        ),
        (
            numeric("What will the price of Bitcoin be?", "Per CoinGecko.", lower=0, upper=500000),
            {**fred("bitcoin", "bitcoin price (USD)", [["2026-08-25", 60000.0], ["2026-09-25", 61000.0]]), "source": "coingecko"},
            True,
        ),
    ],
)
def test_exact_or_stand_in(question, found, exact):
    line = hard_data.official_line(question, found)
    assert line.exact is exact
    assert ("WARNING: stand-in" in line.text) is (not exact)


def test_no_match_or_failed_fetch_gives_no_line():
    assert hard_data.official_line(EXACT_Q, None) is None
    assert hard_data.official_line(EXACT_Q, {"source": "fred", "series": "DGS10", "title": "t", "rule": "r", "error": "HTTPError (HTTP 500)"}) is None


def test_random_walk_range_never_in_the_line():
    found = fred()
    found["baseline"] = {"p05": 1.0, "p95": 9.0}  # set later by the 2b shadow
    assert "1.0" not in hard_data.official_line(EXACT_Q, found).text
    assert "random" not in hard_data.official_line(EXACT_Q, found).text.lower()


# ---------------------------------------------------------------- the dossier


def test_dossier_stays_within_6k_tokens():
    line = hard_data.official_line(EXACT_Q, fred()).text
    long_dossier = " ".join(["word"] * 10_000)
    out = research.with_official_line(long_dossier, line)
    assert out.startswith(line)
    assert len(out.split()) <= int(research.MAX_DOSSIER_TOKENS * research.WORDS_PER_TOKEN)
    short = research.with_official_line("A short dossier.", line)
    assert short == f"{line}\n\nA short dossier."


# ---------------------------------------------------------------- in the bot


def _bot(monkeypatch, parses):
    answers = iter(parses)

    async def fake_structure_output(text, output_type, model=None, additional_instructions=None, num_validation_samples=1):
        return next(answers)

    monkeypatch.setattr(main, "structure_output", fake_structure_output)
    monkeypatch.setattr(main.FallBot2026, "get_llm", lambda self, purpose="default", guarantee_type=None: None)
    return main.FallBot2026(
        llms={"default": "openrouter/x:free", "parser": "openrouter/x:free", "summarizer": "openrouter/x:free", "researcher": "no_research"},
        publish_reports_to_metaculus=False,
    )


def _research_then_parse(bot, question, found, dossier="Dossier text."):
    async def run():
        async def fetch():
            return found

        bot.__dict__.setdefault("_hard_data_tasks", {})[id(question)] = asyncio.create_task(fetch())
        bot._record_for(question)["research_detail"] = {"current_value": None}
        text = await bot._with_official_data(question, dossier)
        result = await bot._parse_numeric_safely(question, "no percentile lines here", "instructions")
        return text, result

    return asyncio.run(run())


def cdf_at(distribution, value):
    points = distribution.declared_percentiles
    return float(np.interp(value, [p.value for p in points], [p.percentile for p in points]))


def test_exact_match_feeds_the_unit_check(monkeypatch):
    # The dossier has no current value; the exact official 4.2 catches the x11 parse.
    bot = _bot(monkeypatch, [X11, RIGHT])
    text, result = _research_then_parse(bot, EXACT_Q, fred())
    assert text.startswith("OFFICIAL DATA (FRED DGS10")
    assert bot._record_for(EXACT_Q)["official_current_value"] == 4.2
    assert cdf_at(result, 4.2) == pytest.approx(0.5, abs=0.02)  # re-parsed


def test_stand_in_does_not_feed_the_unit_check(monkeypatch):
    stand_in = numeric("What will the 10-year Treasury yield be?", "According to Yahoo Finance.")
    bot = _bot(monkeypatch, [X11])
    text, result = _research_then_parse(bot, stand_in, fred())
    assert text.startswith("OFFICIAL DATA") and "WARNING: stand-in" in text
    assert "official_current_value" not in bot._record_for(stand_in)
    assert cdf_at(result, 46.2) == pytest.approx(0.5, abs=0.02)  # accepted as parsed: no 10x check


def test_no_match_leaves_the_dossier_alone(monkeypatch):
    bot = _bot(monkeypatch, [RIGHT])
    text, _ = _research_then_parse(bot, EXACT_Q, None)
    assert text == "Dossier text."
    assert "official_data" not in bot._record_for(EXACT_Q)


def test_slow_or_broken_fetch_never_blocks_research(monkeypatch):
    bot = _bot(monkeypatch, [RIGHT])
    monkeypatch.setattr(main, "HARD_DATA_WAIT_SECONDS", 0.01)
    question = EXACT_Q

    async def run():
        async def slow():
            await asyncio.sleep(5)

        bot.__dict__.setdefault("_hard_data_tasks", {})[id(question)] = asyncio.create_task(slow())
        return await bot._with_official_data(question, "Dossier text.")

    assert asyncio.run(run()) == "Dossier text."


def test_match_rules_unchanged_for_the_pack():
    # 2a's matcher is untouched: the same 4 regression-pack questions match.
    import real_regression

    numeric_pack = [q for q in real_regression.load_questions() if q.question_type in ("numeric", "discrete")]
    assert {q.id_of_post for q in numeric_pack if hard_data.match_question(q)} == {41084, 41083, 41080, 39492}
