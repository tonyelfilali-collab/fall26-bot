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

**The `gemini-free` lineup** (`bot_config.py` `GeminiPool`, active; architect's option A):
- **Forecasting pool:** Gemini 3.6, 3.7, 3.8 and 3.5 Flash (`gemini/...`, called directly with
  `GEMINI_API_KEY`), all with `reasoning_effort="high"` (LiteLLM sends `thinkingLevel: "high"`;
  setting it via `extra_body` gets overwritten with "low").
- **Parser only:** Gemini 3.5 Flash-Lite, then 3.1 Flash-Lite. These never forecast. (The
  architect chose 2.5 Flash / Flash-Lite, but Google answers 404 "no longer available to new
  users" for both on this new project: Credit check run
  https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36336713455.)
- **Daily budget per model:** 20 requests/day each, and every attempt counts (Google counted
  failed ones too: 3 Test Bot runs used up 3.6 Flash's day on 27 Sep). 20% is held in reserve,
  so 16 are usable. Counts are kept per model for the Google day (midnight Pacific = 07:00 UTC
  until 1 Nov, then 08:00 UTC) in `quota/gemini_free.json` in the private `fall26-data` repo
  (`DATA_REPO_TOKEN`), so they carry over between runs (`gemini_budget.py`). Runs never overlap
  (shared concurrency group), so the counts stay correct. A "daily quota exceeded" answer marks
  the model as used up for the day.
- **Per question:** up to 3 forecasts from 3 different models that still have usable budget
  (the models with most budget left go first). When the pool's usable budget is below 25%,
  MiniBench questions get 1 forecast; seasonal questions still get up to 3. If no model has usable
  budget, the question gets 1 forecast from the reserve. Final answer: the library's median
  (binary), mean (multiple choice) or median CDF (numeric). Seasonal questions are forecast
  before MiniBench in each run.
- **No retries on the same model:** a call that finds no budget, an overloaded model (503), a
  quota error or a timeout moves on to another model in the pool not already used for that
  question. The parser gets 2 tries through its chain. Calls are paced at 4/minute per model.
- **Deadline:** a forecast still running 5 minutes before the question closes is cut off (at
  least 30 s is always allowed); finished forecasts are submitted. The question fails only if
  none finished, or if no quota is left.
- **Free-only guard:** `is_free_model` allows OpenRouter `:free` models and `gemini/` models (the
  AI Studio key can't be charged) and rejects everything else, including every paid OpenRouter
  model, backups included.
- **Cost table:** says "Billed: $0 (free tier)", with the list-price equivalent shown separately.
- **Research failure:** if the news search fails, the question is forecast without news rather
  than skipped. On 27 Sep 2026 AskNews started answering **HTTP 402 (Payment Required)** after the
  day's test runs: the free AskNews allowance looks used up.
- **At least 1:** the library normally fails a question with fewer than half the expected
  forecasts; for the Gemini pool this is set to 0 (`required_successful_predictions`), so 1
  successful forecast is enough.
- **Tests:** `tests/test_gemini_budget.py` (19 tests, local dummy Gemini server, no real calls),
  run by the **Unit tests** workflow on every pull request.
- **Test Bot options:** "one binary" (a single binary question) and "only model" (forecast with
  one named Gemini model), to test without using up the free quota.

## Free research check (27 Sep 2026)

**Grounding with Google Search is not available on the free `GEMINI_API_KEY`.** Grounded test calls
(`"tools": [{"google_search": {}}]`) got **HTTP 429 RESOURCE_EXHAUSTED** on `gemini-3.1-flash-lite`,
`gemini-3.5-flash-lite` and `gemini-3.5-flash`. The same models answer normal calls, and their daily
quota wasn't used up, so the free tier allows no grounded requests. Credit check runs:
https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36339235331,
https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36339296741,
https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36339321531.
So there is no free search source while AskNews answers 402; the bot forecasts without news.
(Credit check now has a "grounding" option to re-test this later.)

## Never miss (PLAN.md Step 3, adapted to Plan B)

- **Deadline rule** (`deadlines.py`): the planned forecasts must finish 15 minutes before the
  question closes. Forecasts still running then are dropped and the finished ones are combined
  (median). Inside the last 15 minutes, a forecast gets all the time left minus 2 minutes.
- **Quick forecast:** if no planned forecast finished (errors, overload, time), one quick forecast
  is made with whichever Gemini forecasting model has quota (most left first, reserve allowed),
  ending 2 minutes before the close. Because the free tier is often briefly overloaded, it gets
  up to 3 passes through the models, 30 s apart, while time allows. The question only fails if
  all of those fail.
- **Per-question JSON** (`question_log.py`): after submission, one file per question in
  `fall26-data` at `questions/<mode>/<date>/<post id>_<HHMMSS>.json`: question snapshot, research
  text + time, each forecast (planned or quick, the models that answered, raw output, parsed
  value, seconds, error), final forecast, list-price cost. A save failure never blocks a forecast;
  it logs a warning and a GitHub annotation (`question-log-failed`; `quota-ledger-failed` for the
  ledger).
- **healthchecks.io:** the tournament workflow pings `HEALTHCHECK_URL` at the end of every run
  (with `/fail` if the run failed). No ping when `BOT_ENABLED` is off.
- **Daily health check** (`health.yml` + `health_check.py`) at 07:00 UK time. Two UTC triggers
  (06:00 for BST, 07:00 for GMT); the script runs only for the right one. **Red** if an open
  seasonal/MiniBench question closes within 60 min without our forecast, if there has been no
  successful tournament run for 3 hours, if any Gemini model (forecasters and parsers) has under
  20% of its daily quota left, or if a check can't run. **Warning** if AskNews returns 402, or if
  any tournament run in the last 24 h had a JSON log or ledger save failure. Manual option
  "simulate miss". On pull requests that change the health check it runs with a simulated miss,
  so that run is **expected to be red**.
- Caveat: 07:00 UK is 23:00 Pacific, the end of Google's quota day, so the quota check will
  often be red on busy days.
- Test Bot has a "fail planned forecasts" option to prove the quick forecast.

## Free news fallback (27 Sep 2026)

`free_news.py`, used by `FallBot2026._asknews_with_free_fallback`:
- **AskNews first.** If it fails (e.g. 402) or finds fewer than 3 articles, add free, keyless
  sources: **Google News RSS search** and the **GDELT DOC API**.
- The query is keywords from the question title (stopwords dropped; acronyms like EU and numbers
  like 50 kept; at most 6 words). GDELT gets only words of 3+ letters (its rule), at most 4. If
  fewer than 3 articles are found, Google News is searched again with the first 3 keywords.
- Kept: articles from the last 60 days, duplicates removed, newest first, at most 10. Each has a
  date, source, headline and (if any) a snippet of at most 80 words; the whole text is at most
  2,000 words. Google News RSS gives no real snippet (the description repeats the headline);
  GDELT gives none.
- GDELT allows 1 request per 5 seconds: calls are spaced 6 s apart, with one retry on 429. A
  failing source just contributes nothing.
- **Public log:** only `Question N: articles found: X (AskNews a, free news f)`, never article text.
- Tests: `tests/test_free_news.py` (no network).

## Safety checks (PLAN.md Step 4)

`forecast_safety.py` (pure functions, `tests/test_forecast_safety.py`), used in `main.py`:
- **Binary, fixed order:** median → stretch in log-odds by `STRETCH_K` (**1.0 = off**, architect
  27 Sep: not safe with 1-3 Gemini forecasts until measured; a setting we can turn on) → market
  blend (placeholder, Step 9) → extreme check (below 5% / above 95% only if at least 80% of the
  forecasts that ran are below 10% / above 90%; otherwise pulled back to 10% / 90%) → clip
  **2%–98%**. This replaces the interim 3%–97% clip.
- **Multiple choice:** every option at least 1%, summing to 1 (the library's mean per option; the
  median per option is Step 8).
- **Numeric/discrete:** the combined CDF must follow the platform rules (201 points, or the
  discrete count; increasing by at least 5e-05; steps at most 0.2, bigger for discrete; closed
  bound = 0/1, open bound = at least 0.001 in / at most 0.999). Reversed percentiles are put right.
  If every parsed value is outside the question's range (e.g. a x1000 unit error), the answer is
  re-parsed once with a units warning; if still wrong, a wide fallback distribution across the
  range is used (evenly spread; in log space for log-scaled questions). Not done yet: the
  "median more than 10x or under 0.1x the current value from research" check, because the
  research has no structured current value yet (planner facts, Step 7).
- **Every prompt** includes today's date and the question's close and resolve dates.
- **Before submitting,** the bot re-fetches the question and submits only if it's still open and
  unresolved. The library's own publishing is off; the bot submits itself. If the re-check itself
  fails, it submits anyway rather than risk skipping.
- **Never skip:** if a question fails completely (even the quick forecast), a safe fallback
  forecast is submitted (binary 50% or the community prediction if visible; multiple choice
  uniform; numeric the wide fallback). This makes the run red and leaves a `fallback-forecast`
  annotation, so it gets noticed.

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
