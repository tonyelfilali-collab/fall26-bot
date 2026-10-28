"""
Kimi shadow (architect, 28 Oct, option (a); built OFF: bot_config.KIMI_SHADOW_ENABLED).

The GPT shadow's plumbing (gpt_shadow.py: reservations, caps, spend ledger,
private shadows file, public log, Scoreboard) with another model. A SHADOW
forecast: saved in the private '<log>_shadows.json' only, NEVER submitted.
One call per live question at the end of the run, to OpenRouter
`moonshotai/kimi-k3`:
- pinned to ONE provider endpoint for the whole test (bot_config.
  KIMI_SHADOW_PROVIDER, the cheapest with tool support on 28 Oct; no
  fallbacks), recorded in the shadows file and the ledger call;
- OpenRouter's web search server tool on Exa ($0.007 per search, 5 results),
  at most 3 searches (`max_uses` 3, `max_tool_calls` 3), metaculus.com
  excluded from the results and named in the system note;
- the searches OpenRouter reports (usage.server_tool_use.web_search_requests)
  and the cited domains (url_citation annotations) are saved, like the GPT shadow.

Money: its own share of the spend ledger (`kimi_shadow_days`, calls marked
"shadow": "kimi"), the GPT shadow's caps ($0.25 per question, $1.50 per UTC
day, MiniBench only under $1.00, seasonal first, none under $2 key left).
Each call reserves its worst case first (RESERVATION): a server-tool request
may bill the prompt once per search step, so up to 400k input tokens, 16k
output tokens (reasoning included) and 3 searches.
Public log: "Question N: Kimi shadow <status>, $cost[, domains N, metaculus cited: yes/no]".
"""
from __future__ import annotations

import bot_config
from gpt_shadow import METACULUS_DOMAIN, GptShadow, GptShadowLlm, ShadowSpec

MODEL = bot_config.KIMI_SHADOW_MODEL  # openrouter/moonshotai/kimi-k3
LEDGER_MODEL = MODEL.removeprefix("openrouter/")
PROVIDER = bot_config.KIMI_SHADOW_PROVIDER
VARIANT = "kimi-shadow"  # its name in the shadows file's "shadow" variants
# Dollars per million tokens, rounded up from the pinned endpoint (28 Oct: $0.1577 / $9.7439).
PRICE_INPUT, PRICE_OUTPUT = 0.17, 9.75
SEARCH_PRICE = 0.007  # dollars per Exa search (up to 10 results; we ask 5)
MAX_SEARCHES = 3
RESULTS_PER_SEARCH = 5
CHARACTERS_PER_RESULT = 2_000
# Reserved per call: the prompt billed once per step (up to 4 steps with 3
# searches), ASSUMED at most 400k input tokens in all; output capped at 16k.
RESERVE_INPUT_TOKENS = 400_000
MAX_OUTPUT_TOKENS = 16_000
RESERVATION = round(
    (RESERVE_INPUT_TOKENS * PRICE_INPUT + MAX_OUTPUT_TOKENS * PRICE_OUTPUT) / 1_000_000 + MAX_SEARCHES * SEARCH_PRICE, 6
)  # 0.068 + 0.156 + 0.021 = $0.245
TIMEOUT_SECONDS = 240  # then cancelled and charged the reservation (also capped by the run's minute 47)
REASONING = {"effort": "medium"}
# Test Bot 40730415966: with "use the research given below first" Kimi made no
# search on 4 of 4, so the note asks for searches (like agent 1's floor).
SEARCH_NOTE = (
    f"Before answering, search the web: make 2 to {MAX_SEARCHES} searches (at most {MAX_SEARCHES} searches in all), "
    "for the latest news on the question and for the resolution source named in the criteria. "
    "Do not use metaculus.com. Then combine what you find with the research given below."
)


def request_extras() -> dict:
    """The OpenRouter-only request fields (sent as the body's top level)."""
    return {
        "reasoning": dict(REASONING),
        "tools": [{
            "type": "openrouter:web_search",
            "parameters": {
                "engine": "exa", "max_uses": MAX_SEARCHES, "max_results": RESULTS_PER_SEARCH,
                "max_total_results": MAX_SEARCHES * RESULTS_PER_SEARCH, "max_characters": CHARACTERS_PER_RESULT,
                "excluded_domains": [METACULUS_DOMAIN],
            },
        }],
        "max_tool_calls": MAX_SEARCHES,
        "provider": {"order": [PROVIDER], "allow_fallbacks": False},
    }


KIMI = ShadowSpec(
    name="Kimi shadow", tag="kimi", config="KIMI_SHADOW", variant=VARIANT, price_input=PRICE_INPUT,
    price_output=PRICE_OUTPUT, search_price=SEARCH_PRICE, reservation=RESERVATION, max_output_tokens=MAX_OUTPUT_TOKENS,
    timeout=lambda: TIMEOUT_SECONDS, extras=request_extras, search_note=SEARCH_NOTE, provider=PROVIDER,
)


class KimiShadow(GptShadow):
    """The run's Kimi shadow: the GPT shadow's caps and ledger handling, its own spec."""

    spec = KIMI

    def __init__(self, spend, key_left, enabled=None, llm_class=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(spend, key_left, enabled, llm_class or KimiShadowLlm)


class KimiShadowLlm(GptShadowLlm):
    """One guarded OpenRouter call (no retries, no backup) for one question's Kimi shadow."""
