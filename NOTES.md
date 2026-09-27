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

## Plan B: free Gemini key (27 Sep 2026)

Secret `GEMINI_API_KEY`: Google AI Studio free tier, no billing.

**What the key can use** (Credit check run on the Plan B branch,
https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36331438194):
- It lists 30+ Gemini models, including `gemini-3.6-flash`, and also the newer `gemini-3.7-flash`,
  `gemini-3.8-flash`, `gemini-3.5-flash`, `gemini-3.1-pro-preview` and `gemini-2.5-pro`.
- One test call to `gemini-3.6-flash` with thinking level "high" worked: HTTP 200, 146 thinking
  tokens for a one-word answer, so high reasoning is supported and on.
- **Rate limits (AI Studio rate-limit page, 27 Sep 2026):** Gemini 3.6 Flash free tier = **5
  requests/minute, 250K tokens/minute, 20 requests/day**. Limits are per project (not per key),
  and the day resets at midnight Pacific (07:00 UTC in summer time). Limits are listed per
  model, so other Gemini models have their own separate quotas.
- The **first** `GEMINI_API_KEY` belonged to a project on a **paid tier** (1K/minute, 10K/day,
  847 requests already used that day by something else, so billing was on). It was replaced
  with a key from a new project on the free tier before the bot went live. About 42 of our test
  calls went to the old project (list-price equivalent about $0.67).
- **Daily budget:** 20/day, keep 20% in reserve = 16 calls; about 8 questions/day means 2 calls
  per question = **1 forecast + 1 parse**. `bot_config` computes this
  (`GEMINI_FREE_FORECASTS_PER_QUESTION` = 1). The pace is 4/minute and each call gets 2 tries.

**The `gemini-free` lineup** (`bot_config.py`, now the active lineup):
- `gemini/gemini-3.6-flash` (LiteLLM calls Google directly with `GEMINI_API_KEY`), forecasts
  per question set by the daily budget (now 1) with `reasoning_effort="high"`, which LiteLLM sends as `thinkingLevel: "high"`
  (checked against a dummy server). Setting `thinkingConfig` through `extra_body` does **not**
  work: LiteLLM overwrites it with `"low"`.
- Parser: the same model, 1 parse per forecast. No research summary. AskNews research as before.
- Pacing: every call (forecasts, parses, retries) waits its turn on its model's pacer,
  `GEMINI_FREE_REQUESTS_PER_MINUTE` = 4 (`llm_throttle.py`). 2 tries per call with backoff.
- Backups: the free tier gets "503 model overloaded" at busy times (the first budget Test Bot run
  failed 3 of 4 questions that way). If a model is overloaded, out of quota, times out or has a
  server error, the call goes to `gemini-3.7-flash`, then `gemini-3.5-flash`. Each has its own
  free quota on the same key, and all use high reasoning. Checked against a dummy server:
  thinkingLevel "high" is sent for all three, and a 503 on 3.6 falls over to 3.7.
- Deadline: a forecast still running 5 minutes before the question closes is cut off (at least
  30 s is always allowed). The forecasts already made are combined and submitted. The question
  only fails if none finished.
- Free-only guard: `is_free_model` allows OpenRouter `:free` models and `gemini/` models (Google
  AI Studio key, can't be charged), and rejects everything else, including every paid OpenRouter
  model. `gemini-free` may run on real questions (still behind `BOT_ENABLED`); the OpenRouter
  `free` lineup stays testing-area only.
- Cost table: LiteLLM prices Gemini 3.6 Flash at list price ($0.75/M input), so the run summary
  says "Billed: $0 (free tier)" and shows the list-price equivalent separately.

## Three checks (PLAN.md Step 1)

1. **When are forecasts submitted?** Each question's forecast is submitted as soon as that
   question is finished, not at the end of the run. `ForecastBot._run_individual_question`
   calls `report.publish_report_to_metaculus(...)` right after aggregating that question.
   Questions run in parallel (`asyncio.gather`). One catch: in tournament mode `main.py` runs the
   seasonal tournament first and MiniBench after it, so MiniBench questions wait for all seasonal
   questions to finish.
2. **Fall MiniBench slug/id and how long questions stay open** (Tournament info run, 27 Sep 2026,
   https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36330636832):
   - `minibench` resolves to **id 33125**, name "MiniBench", start 21 Sep 2026, close 9 Oct 2026:
     the current round (a bit under 3 weeks). Our code uses the slug `minibench`. If Metaculus
     moves that slug to each new round, we follow automatically; if it doesn't, we would keep
     pointing at 33125. Re-run **Tournament info** after 9 Oct to see which.
   - It had **0 open questions**, so how long MiniBench questions stay open is **not known yet**.
     Re-run Tournament info while MiniBench questions are open.
   - Seasonal: `fall-futureeval-2026` = id 33121, "Fall 2026 FutureEval Bot Tournament", start
     28 Sep 2026, project close 5 Mar 2027 (PLAN.md says the season ends 6 Jan 2027; the project
     close date is later). It already had 1 open question: 45516 (discrete), open 6 Sep 06:00 UTC,
     closing 28 Sep 05:59 UTC. The bot is off (`BOT_ENABLED` unset, free lineup), so it won't
     forecast it.
3. **AskNews calls per question:** `asknews/news-summaries` makes **2** calls per research report
   (`search_news` with strategy "latest news", then "news knowledge"), and there is 1 research
   report per question, so **2 calls per question**. Source: `AskNewsSearcher.get_formatted_news_async`
   in forecasting-tools 0.3.1. It waits between the two calls, because the free tier allows 1 call
   per 10 s. No retries, so 2 is the maximum (limit: 3).

## Workflows and what triggers them (Task 4)

All run on `ubuntu-24.04` (pinned; `ubuntu-latest` moves to Ubuntu 26 on 19 Oct 2026) with
`actions/checkout@v7` and `actions/setup-python@v7` (Node 24).

| Workflow | File | Triggers | Can spend / forecast real questions? |
|---|---|---|---|
| Forecast on new AI tournament questions | `run_bot_on_tournament.yaml` | schedule `7,27,47 * * * *` and `17,37,57 * * * *`; manual | Yes, but only if `BOT_ENABLED` is `true`; otherwise logs "BOT_ENABLED is not true, exiting" and stops before checkout |
| Forecast on Metaculus Cup | `run_bot_on_metaculus_cup.yaml` | manual only | Same `BOT_ENABLED` gate |
| Test Bot | `test_bot.yaml` | manual only (option: break on purpose) | bot-testing-area only; free lineup costs $0 |
| Review recent forecasts | `review_bot.yaml` | schedule Mondays 06:00 UTC; manual | Never spends or forecasts (read-only). The schedule also needs `REVIEW_BOT_ENABLED` and `BOT_ENABLED` both `true` |
| Credit check | `credit_check.yaml` | manual only | No: reads the key's limit/usage, runs no model |
| Tournament info | `tournament_info.yaml` | manual only | No: read-only Metaculus API |
| Keepalive | `keepalive.yaml` | schedule Mondays 05:00 UTC; manual | No: one empty commit to `main` so schedules never pause |

Extra safety in code: while `bot_config.USE_CREDIT_KEY_LINEUP` is `False`, `main.py` refuses to run
any mode except `test_questions`, and the free lineup rejects any non-`:free` model.

The three forecasting workflows share one concurrency group (`forecast-bot`) and have a
60-minute timeout. GitHub keeps at most one run waiting in a group: if a second run queues up
behind it, the older waiting run is cancelled (the running one never is).

## Logs (Task 4)

- Our code logs only question id, status and cost (`fall26` logger).
- Every library log line at WARNING or above is shown as `[message hidden: public repo]` with the
  logger name, plus the error types if there was an exception. Library INFO/DEBUG lines are dropped.
- Errors show types and file:line (e.g. `RuntimeError at main.py:171`), never messages, because
  library error messages can quote model output.
- Trade-off: when something breaks, the public log says where but not why. Full details will go
  to the private `fall26-data` repo (PLAN.md Step 3).
- A run fails (red) if any question failed (`sys.exit(1)`), after writing the cost table.

## Credit check result (27 Sep 2026)

https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36330634993
The current `OPENROUTER_API_KEY`: limit **no limit**, used **$0**, remaining **no limit**, free-tier
key **true**. "No limit" means the key itself has no spending cap. Zero spend currently relies
on the account having no credit, plus the free-only lineup guard. When the Metaculus credit key
is in, run Credit check again: its limit should show the donated amount.

## Other things found

- The fork has no workflows registered and no secrets yet. Forked repos keep Actions workflows
  off until someone enables them on the Actions tab.
- Local development uses Python 3.11 (same as Actions). The old aiohttp in the lock file breaks
  on Python 3.14.
