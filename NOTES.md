# NOTES

Findings from Task 1 (checked 2026-09-27).

## forecasting-tools version

- Now **0.3.1** (latest on PyPI). The lock file had 0.2.92.
- 0.2.92 still pointed at Summer 2026. 0.3.1 is the first line with Fall 2026 built in.
- Blocker we worked around: `metaculus-bot-review` 0.1.1 (optional review tool) requires
  forecasting-tools `<0.3`. Upstream hit the same wall and stayed on 0.2.92.
  We removed it from `pyproject.toml`; the review workflow now runs it in its own
  isolated install with `pipx run`, so both keep working.

## Tournament constants (in `MetaculusClient`, 0.3.1)

| Constant | Value | Fall 2026? |
|---|---|---|
| `CURRENT_AI_COMPETITION_ID` | `FE_FALL_2026_ID` = 33121 (`fall-futureeval-2026`) | Yes |
| `CURRENT_MINIBENCH_ID` | `"minibench"` (a generic slug, not a season-specific one) | Depends on what Metaculus points that slug at; confirm in an Actions run (the Metaculus API blocks requests from outside without a token) |
| `CURRENT_METACULUS_CUP_ID` | `METACULUS_CUP_FALL_2026_ID` = 33108 | Yes |

- Our `main.py` still uses the SDK constant and Summer URLs. Task 2 hard-codes
  `fall-futureeval-2026` instead.
- bot-testing-area is id 32977 (used by `--mode test_questions` via the slug `bot-testing-area`).
- 0.3.1 ships an official `FallTemplateBot2026` ("identical to SummerTemplateBot2026") with one
  useful change: for `asknews/news-summaries` it sends only the question text as the search query,
  not the long assistant prompt. Worth copying in Task 3.

## Secret names the code reads

Our code (`main.py` → `bot_helpers.check_environment`):
- `METACULUS_TOKEN` (required; the run stops without it)
- `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` (only checks that one exists)

The library (forecasting-tools 0.3.1):
- `METACULUS_TOKEN`: Metaculus API, and the `metaculus/` LLM proxy
- `OPENROUTER_API_KEY`: any `openrouter/...` model
- `ASKNEWS_CLIENT_ID` + `ASKNEWS_SECRET` (preferred), **or** `ASKNEWS_API_KEY`: AskNews research
- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `PERPLEXITY_API_KEY`, `EXA_API_KEY`: other providers
  (we will not use these; ZERO SPEND)

Before Task 3 the workflows passed: `METACULUS_TOKEN, PERPLEXITY_API_KEY, EXA_API_KEY, OPENAI_API_KEY,
OPENROUTER_API_KEY, ANTHROPIC_API_KEY, ASKNEWS_CLIENT_ID, ASKNEWS_SECRET`. They do **not** pass
`ASKNEWS_API_KEY`.

**Secrets in the repo (27 Sep 2026): `METACULUS_TOKEN`, `OPENROUTER_API_KEY`, `ASKNEWS_API_KEY`.**
AskNews uses **one API key** (`ASKNEWS_API_KEY`), not a client ID + secret. The library accepts
either. PLAN.md section 2 still lists `ASKNEWS_CLIENT_ID`/`ASKNEWS_SECRET`; the architect should
update it. Since Task 3 the workflows pass only these three secrets.

**Danger:** if no `llms=` are configured, the library picks defaults by which keys exist. With
`OPENROUTER_API_KEY` set, it picks `openrouter/openai/gpt-4o` (paid) for forecasting, and
`gpt-4o-mini` (paid) for parsing and summaries. With a $0 key these calls fail. With the credit key
they spend. Task 3 must set every model explicitly.

## How several predictions are combined

Each question gets `research_reports_per_question × predictions_per_research_report`
predictions (currently 1 × 5 = 5), then:

- **Binary:** median of the probabilities. We also clamp each prediction to 1%–99% before that.
- **Multiple choice:** mean (average) probability per option. All predictions must list the
  same options.
- **Numeric / discrete / date:** each prediction becomes a CDF on the same x-axis; the
  **median at each point** of the CDF is taken.

## OpenRouter model IDs and "high reasoning"

With LiteLLM, prefix each with `openrouter/`. Prices are per million tokens (input / output).

| Model | OpenRouter ID | Price |
|---|---|---|
| Claude Opus 5.5 | `anthropic/claude-opus-5.5` | $4 / $20 |
| Claude Fable 5.1 | `anthropic/claude-fable-5.1` | $10 / $50 |
| GPT-5.6 Sol | `openai/gpt-5.6-sol` | $2 / $10 |
| Gemini 3.6 Flash | `google/gemini-3.6-flash` | $0.75 / $3.75 |

All four support OpenRouter's `reasoning` parameter. To ask for high reasoning, use
OpenRouter's unified form:

```python
GeneralLlm(
    model="openrouter/anthropic/claude-opus-5.5",
    temperature=None,  # leave unset for reasoning models
    extra_body={"reasoning": {"effort": "high"}},
)
```

Tested locally against a dummy server (no real call, $0): `extra_body={"reasoning": {...}}`
and `reasoning={"effort": "high"}` both send `"reasoning": {"effort": "high"}`.
`reasoning_effort="high"` sends the OpenAI-style top-level `reasoning_effort` instead.
We use the `extra_body` form because it is OpenRouter's documented one and works the same
for all four.

LiteLLM (1.80.10) has prices for all four, so cost tracking works
(`ForecastReport.price_estimate`, per question).

Free models that support structured output (candidates for the `:free` forecaster):
`nvidia/nemotron-3-super-120b-a12b:free`, `qwen/qwen3.8-27b:free`.

## Model settings (Task 3)

All model choices are in `bot_config.py`:
- **Free lineup (active):** `openrouter/nvidia/nemotron-3-super-120b-a12b:free` as forecaster and
  parser. 1 forecast per question, 1 parse, no research summary, to stay inside free rate limits.
  The code refuses to use a non-`:free` model in this lineup, and refuses to run any mode except
  `test_questions` with it.
- **Credit lineup (prepared, off):** Claude Opus 5.5 with high reasoning, timeout 600 s, 3 tries,
  5 forecasts per question; Gemini 3.6 Flash as parser and summarizer.
  Switch with `USE_CREDIT_KEY_LINEUP = True`, only once the credit key is in.
- **Research:** AskNews news summaries. Only the question text is sent as the search query
  (as in Metaculus's `FallTemplateBot2026`).
- A test run forecasts the first open question of each type (binary, numeric, discrete,
  multiple choice) in the bot-testing-area, not all of them.
- The Actions run summary shows a cost-per-question table (id, type, status, cost only).

## Three checks (PLAN.md Step 1)

1. **When are forecasts submitted?** Each question's forecast is submitted as soon as that
   question is finished, not at the end of the run. `ForecastBot._run_individual_question`
   calls `report.publish_report_to_metaculus(...)` right after aggregating that question.
   Questions run in parallel (`asyncio.gather`). One catch: in tournament mode `main.py` runs the
   seasonal tournament first and MiniBench after it, so MiniBench questions wait for all seasonal
   questions to finish.
2. **Fall MiniBench slug/id and how long questions stay open:** not yet answered. It needs an
   Actions run with the Metaculus token.
3. **AskNews calls per question:** `asknews/news-summaries` makes **2** calls per research report
   (`search_news` with strategy "latest news", then "news knowledge"), and there is 1 research
   report per question, so **2 calls per question**. Source: `AskNewsSearcher.get_formatted_news_async`
   in forecasting-tools 0.3.1. It waits between the two calls, because the free tier allows 1 call
   per 10 s. No retries, so 2 is the maximum (limit: 3).

## Other things found

- The fork has no workflows registered and no secrets yet. Forked repos keep Actions workflows
  off until someone enables them on the Actions tab.
- Local development uses Python 3.11 (same as Actions). The old aiohttp in the lock file breaks
  on Python 3.14.
