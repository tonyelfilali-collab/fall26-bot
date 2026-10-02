# NOTES

## What's live (switches and settings, 29 Sep 2026)

Every on/off switch and key setting, its current value, what it does, and the PR. Change one = a PR
(the tier rules in CLAUDE.md apply). Emergency stop: repo variable `BOT_ENABLED` = `false`.

| Switch / setting | Where | Now | What it does | PR |
|---|---|---|---|---|
| `BOT_ENABLED` | repo variable | `true` | Master switch: real-question workflows exit at once unless `true` | #15 |
| `ACTIVE_LINEUP` | `bot_config.py` | `gemini-free` | Free Google AI Studio key: 4 Flash forecasters, Flash-Lite for research/parsing | #8, #9 |
| Credits lineup | `bot_config.py` (`credits`) | OFF (built) | Opus 5.5 / GPT-5.6 Sol / Gemini 3.6 Flash tiers; Flash slot tries the free key first; switch-on checklist PLAN.md 6b | #23, #74, #79 |
| Gemini daily limits | `bot_config.GEMINI_DAILY_LIMITS` | Flash 20, Flash-Lite 400 (3.1-preview shares 3.1) | Per-model quota ledger; every attempt counts | #60, #69 |
| Reserve | `GEMINI_FREE_RESERVE` | 20% | Held back for a question's first forecast, emergencies, research | #9 |
| `EXTRA_FORECASTS_ENABLED` | `bot_config.py` | ON | Up to 5 (binary) / 6 (numeric, MC) forecasts when non-reserve quota is spare; 25 questions/day expected while MiniBench is active | #85, #89 |
| `PAID_FLASH_FALLBACK` | `bot_config.py` | **ON** (Tony, 30 Sep; key limit $8) | gemini-free: a Flash chain that can't answer ends with one paid OpenRouter Gemini 3.6 Flash call, only until the question has 3 real forecasts (extra slots free only); caps $0.09/question, $1.00/UTC day (MiniBench only under $0.50); fail closed | this PR |
| Backup chain | `BACKUP_FORECASTS`, `BACKUP_FORECAST_MODEL` | 2 x Nemotron Ultra `:free` | No Flash answer and (closing within 45 min or all Flash out of quota): Nemotron x2 first, Flash-Lite only if Nemotron gives nothing | #67 |
| Emergency window | `main.EMERGENCY_WINDOW` | 45 min | When the backup chain may use the Flash-Lite reserve | #59, #67 |
| `STRETCH_K` | `forecast_safety.py` | 1.0 (off) | Log-odds stretch of the binary median (shadows 1.2 / 1.5 are logged) | #16, #44 |
| `MARKET_MODE` | `bot_config.py` | `off` | Market matching (code kept; manual Market matches workflow) | #26, #30 |
| `REFEREE_ENABLED` | `shadow.py` | `False` | Referee shadow (waits for credits) | #27 |
| `FOLLOWUP_ENABLED` | `followup.py` | ON | Round 1 disagrees: 2 follow-up queries, "Follow-up findings" section, round 2 uses it | #86 |
| `WIKIPEDIA_ENABLED` | `wikipedia.py` | ON | Up to 3 entity summaries as "Background (Wikipedia)" (<= 800 tokens) | #87 |
| Official data line | `hard_data.py` (no switch) | ON | FRED / CoinGecko line in the dossier; exact match feeds the unit check; stand-in warning | #62, #68 |
| `YAHOO_ENABLED` | `hard_data.py` | ON, but Yahoo answers 429 | Stocks/indices/commodities/FX via Yahoo; blocked, skipped gracefully (stocks not covered) | #87 |
| Shadow forecaster | `SHADOW_FORECAST_MODEL` | Nemotron Ultra `:free` | After each live forecast; never submitted; scored | #61 |
| Zero-cost shadows | `shadow.py`, `window_baseline.py` | ON | Stretch, mean, geo-mean, trimmed-mean, MC mean, numeric mean-cdf/uniform, window baseline (replaced the random walk, 1 Oct) | #27, #44, #63, this PR |
| Consistency shadow | `consistency.py` (no switch) | ON | Sibling ladders; isotonic "consistent" shadow; never submitted | #78 |
| Replay lab | `lab.py`, workflow Replay lab | ON (daily, last 3 h Pacific) | E1-E4 on frozen dossiers after resolution; Nemotron <= 300/day; Gemini only spare, never reserve | #92 |
| Spend guards | `spend.py` | ON (credits only) | Per-question cap 2x tier cost, daily cap 2x target, key backstop, never re-buy, fail closed | #81 |
| Health check start | `health_dispatch.py` | ON | First tournament run after 06:00 UTC starts health.yml | #84 |
| MiniBench discovery | `minibench.py` | ON | Slug + remembered rounds + 20-id scan every run; newest running round | #41, #90 |
| Run timing | `run_timing.py` | ON | One run queue, research soonest-closing first; research 4 min; forecasting stages 12 min (planned forecasts waited for until minute 9); no new question after 30 min; shadows at the run's end only before minute 45 (end by 47); log saved at submission, shadows in `<log>_shadows.json` | #96, this PR |


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
- **Daily budget per model (AI Studio page, 28 Sep):** Flash 20 requests/day each, 5/minute
  (paced at 4); **Flash-Lite 500/day each, 15/minute** (the ledger plans with 400/day, paced at
  12); `gemini-3.1-flash-lite-preview` has no row of its own and shares 3.1-flash-lite's quota
  (`GEMINI_QUOTA_BUCKETS`). 20% of each is held in reserve (16 of 20, 320 of 400 usable).
  **Every attempt is counted, failed ones too** (28 Sep: Google counted ~20 failed 503 attempts
  per Flash model against the 20/day, then answered 429). A model is also used up when Google
  answers 429 RESOURCE_EXHAUSTED. A model that fails twice in a run is skipped for the rest of
  the run; a question may try each model at most 4 times a day; a question that failed is retried
  in the next run, then every 30 minutes, and in every run in its last 45 minutes (retry backoff,
  `fall26-data/status/retry_state.json`). Calls in progress hold a slot so parallel forecasts can't overbook. Counts are kept per model for the Google day (midnight Pacific = 07:00 UTC
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
  successful tournament run for 3 hours, or if a check can't run. **Warning** if any Gemini model
  (forecasters and parsers) has under 20% of its daily quota left (architect, 27 Sep: warning
  only), if AskNews returns 402, or if any tournament run in the last 24 h had a JSON log or
  ledger save failure. Since 1 Oct also **red** if any workflow's state (Actions API) isn't "active"
  (named; "deleted" = file removed, not counted) and a **warning** if main's last commit is older than
  21 days (standing rule: a notes PR at least every 21 days). Manual option
  "simulate miss". On pull requests that change the health check it runs with a simulated miss,
  so that run is **expected to be red**.
- 07:00 UK is 23:00 Pacific, the end of Google's quota day, so the quota warning will be common.
- Test Bot has a "fail planned forecasts" option to prove the quick forecast.
- **Test Bot never uses the live Gemini quota:** by default it runs the `free` lineup (OpenRouter
  `:free` models, testing area only). Its `lineup` option can pick `gemini-free` on purpose.
  Credit check makes no Gemini test call unless models are named.

## Backup chain: Flash-Lite + Nemotron (Build 3, 29 Sep 2026)

When no Gemini Flash forecaster answered (`main.py` `_backup_forecasts`, `bot_config.GeminiPool`),
in two cases: the question **closes within 45 min**, or **every Flash model is out of quota** for
the current quota day (429, or ledger at 0; `GeminiPool.flash_exhausted`; the day follows
`gemini_budget.quota_day`, Pacific midnight; a 503 is not exhaustion):
- **Nemotron first** (`BACKUP_FORECAST_MODEL`, the `:free` shadow model): 2 forecasts at the same
  time (`BACKUP_FORECASTS`), median of what answers, then the normal checks.
- **Only if Nemotron gives no answer:** up to 2 Flash-Lite forecasts. In the window they may use
  the reserve; earlier only quota ABOVE the reserve, and answers are parsed above it too.
- Question log `emergency`: `window: nemotron`, `window: nemotron failed -> flash-lite`,
  `flash-exhausted: nemotron`, `flash-exhausted: nemotron failed -> flash-lite`, or
  `...: nemotron failed, no flash-lite`.
- Nemotron: 240 s limit, 1 try each, not in the Gemini ledger (OpenRouter free requests). Its
  failure never blocks Flash-Lite.
- Test Bot `lineup=replay-gemini` (+ `exhaust_flash`): the real pool logic with recorded replies,
  0 model calls, ledger in memory only; the job summary shows the chain used and whether the
  Flash-Lite reserve was touched.
## Official data line in the dossier (Build 2c partial, 29 Sep 2026)

For a numeric/discrete question matched to FRED or CoinGecko (`hard_data.match_question`, 2a), ONE
line goes first in the dossier (`main._with_official_data`, `hard_data.official_line`): series name
and id, latest value and date, past-year min/max, 30-day change. The dossier is cut to keep the
total within ~6,000 tokens (`research.with_official_line`).
- **Exact** (the latest value feeds the unit check, #34, instead of the dossier's own CURRENT
  VALUE): no stand-in reason below.
- **Stand-in** (line shown with `WARNING: stand-in: ...`, unit check NOT fed): the question asks
  for an intraday value / maximum / minimum / average; or it names another data site (Yahoo
  Finance, Bloomberg, Coinbase...) and not FRED / the series id / the official publisher; or a
  coin question doesn't name CoinGecko; or the latest value is outside the question's range
  (units may differ).
- The window baseline (replaced the 2b random walk, 1 Oct) is never in the dossier: it stays a shadow until scored.
- Research waits at most 45 s for the fetch; a failure or no match = no line.
- Question log: `official_data` (line, exact), `official_current_value` (exact only).

## Credits readiness (29 Sep 2026; credits lineup still OFF)

- **4a pre-flight** (`preflight.py`, Credits pre-flight workflow): every `ensemble.py` model id vs
  OpenRouter's public list (exists, reasoning, prices); free models must be $0.
- **4b cost table** (`cost_table.py`, same workflow): real prices x prompt tokens measured from
  question logs + ASSUMED 8k output per forecast. `ensemble.COST_TABLE`; tier cost = its most
  expensive column (full $0.64, standard $0.39, lean $0.26 on 28 Sep, after Fable 5.1 was removed:
  full round 2 = one more Opus 5.5, GPT-5.6 Sol and Gemini 3.6 Flash; Fable only in the pre-flight price list).
- **4c free first:** the Flash 3.6 slot tries the AI Studio key first (`CREDITS_FREE_FIRST`),
  OpenRouter only when it can't answer; planner/dossier/parser/summarizer on the free Flash-Lite pool.
- **4d spend guards** (`spend.py`), all fail closed: each paid call's real OpenRouter cost (LiteLLM
  passes OpenRouter's `usage.cost` through) goes to `fall26-data/status/spend.json` (per UTC day, per
  question). A success with no cost (LiteLLM reports 0) is charged the estimate and counted "cost
  unknown" (alert issue, once a day). A failed call is charged the estimate unless rejected before
  generating (HTTP 400/401/402/403/404/429). A paid call with no question context is refused.
  Per-question cap = 2x tier cost (each running call reserves an estimate: real prices, 7.5k prompt,
  ASSUMED 8k output); at the cap no new paid forecast starts. A timeout means that model is not tried
  again on the question. Each credits run, before forecasting (`guard_tier`): Lean if the ledger is
  missing/unreadable (that run; an unreadable ledger is never overwritten), if today's spend reached 2x
  the target daily spend (rest of the UTC day), if the key's own usage since the day's first run
  exceeds the ledger's day total by more than max(20%, $0.50) (rest of the day), or if the key's usage
  can't be read (that run); each with an alert issue (title carries the date; not reopened while open).
  Unknown credit -> Lean. Paid models: 2 tries of the chain, 5 s apart; a reached cap is never retried.
  Finished forecasts are kept 3 days (`status/finished_forecasts.json`) and reused on a retry run.
  Rehearsals: Test Bot `replay-credits` + `rehearsal` = runaway / retry-run / unknown-cost /
  missing-ledger / key-mismatch.

## Related-question consistency shadow (29 Sep 2026)

Zero cost, never submitted (`consistency.py`). After each submitted binary forecast, the question
goes into an index of our open binary forecasts (`fall26-data/status/binary_forecasts.json`, live
tournament runs only). Siblings = same tournament, titles identical except ONE number or ONE date.
Direction from the words before it: above/exceed/at least... -> a higher threshold never gets a
higher probability; below/under/at most... -> never lower; by/before/until a date -> a later
deadline never lower; otherwise no direction (logged only). The isotonic (pool adjacent violators)
adjustment of our submitted forecasts is saved as shadow `consistent` (scored by the Scoreboard);
the group and violation count go to the question log (`consistency`), counts only in the public log.
The value is computed when the question is submitted (siblings submitted later don't update it).

## More forecasts when quota is spare (Build 4, 29 Sep 2026)

`bot_config.EXTRA_FORECASTS_ENABLED` (default ON). A seasonal question (or MiniBench when the budget
isn't low) gets 3 forecasts by default, up to 5 (binary round 1) or 6 (numeric / multiple choice)
when the usable, non-reserve Flash quota left AFTER this question is at least 4 x the questions still
expected today = max(7-day average of live questions per day, 4) x the share of the Pacific quota
day left, + 2. The 7-day average comes from the question-log paths in fall26-data
(`question_log.questions_per_day`, each live run). Distinct models first, then repeats (most usable
quota first); extras never use the reserve. Question log `extra_forecasts` = {enabled, count, reason};
the public log shows count and reason (quota counts only).
While a MiniBench round is active (a question open now, or seen open in the last 24 h, kept as
`last_open_seen` in `status/minibench.json`), the expected count uses 25 a day (Build 4b).

## Disagreement follow-up search (Build 5, 29 Sep 2026)

`followup.FOLLOWUP_ENABLED` (default ON). After round 1, if the forecasts disagree (binary: spread
over 15 points; numeric/discrete: medians differ by over 25% of the range; multiple choice: the top
option differs) and the question doesn't close within 25 min: the parser model (Flash-Lite) reads each
model's two-line reason and writes up to 2 queries; AskNews while a call is left under the
3-per-question cap (from `research_detail.asknews_calls`), otherwise free news. A "Follow-up findings"
section (at most 1,500 tokens) is added and the dossier trimmed so the whole stays within ~6k tokens.
Round 2 uses it: binary round 2 as before; numeric / multiple choice get a round 2 (Gemini pool,
`round2`: up to 2 more models, usable quota only) only after a follow-up search. Question log
`followup` = {enabled, triggered, trigger, queries, counts}. Replay: frozen section, no search.

## Wikipedia background and Yahoo Finance data (Build 6, 29 Sep 2026)

- **6a Wikipedia** (`wikipedia.py`, switch `WIKIPEDIA_ENABLED`, default ON): the research planner
  also names up to 3 entities (English Wikipedia titles); their REST summaries (free, no key; our
  User-Agent) are added at the end of the dossier as "## Background (Wikipedia, retrieved <date>)",
  at most ~800 tokens in all; the dossier is cut so the whole stays within ~6k. A failed or
  disambiguation page is skipped. Question log `research_detail.wikipedia` = page titles.
- **6b Yahoo Finance** (`hard_data.py`, switch `YAHOO_ENABLED`, default ON): after the FRED and
  CoinGecko rules, indices by name (Nikkei 225, FTSE 100, DAX, Hang Seng, Russell 2000, Euro Stoxx 50,
  CAC 40) and, with a price/level word, commodities (gold, silver, copper, natural gas futures), FX
  (EUR/USD, USD/JPY, GBP/USD, USD/CNY), big stocks by name, or a ticker written "(NYSE: KO)". Data:
  Yahoo's public chart endpoint (the one the yfinance package reads; called directly with `requests`,
  so no pandas/yfinance dependency), ~1 year of daily closes. Same dossier line as FRED/CoinGecko;
  stand-in warning rules apply, plus "futures price vs spot price". A block (e.g. HTTP 429) is saved
  as an error: no line. The Credit check workflow also checks Yahoo (gold) and Wikipedia.

## Run timing (29 Sep 2026)

Architect, Tier B: a burst of questions must never push a run past the workflow's 60-minute limit
(`run_timing.py`, used by `main.py`). All times are event-loop seconds from the process start.
- **One run queue:** seasonal and MiniBench questions go into one `forecast_questions` call (the old
  batches, where MiniBench closing within 30 min went first and each batch waited for the one before,
  are gone). Each question keeps its own seasonal/MiniBench flag (`seasonal_by_post`), so seasonal
  keeps its quota priority in planning.
- **Research order:** one question researches at a time (`ResearchQueue`, replacing the template's
  semaphore); the turn always goes to the soonest-closing question that hasn't researched yet.
  Forecasting runs side by side, as before.
- **Research: 4 min per question.** Planned research stops at the deadline in whichever step it's
  in (plan, search, dossier, gap-fill, Wikipedia) and the dossier is built from what was gathered
  (the articles if the dossier wasn't written). Question log `research_time_limit` = that step. The
  official-data wait counts in the 4 minutes. Any other research is cut 15 s after the limit
  (`research_time_limit: cut`, forecast without research).
- **Forecasting stages: 12 min per question**, from the end of its research: planned forecasts,
  the quick forecast, the follow-up search and round 2. Planned forecasts are waited for only until
  minute 9 (what finished is used), so if none finished the quick forecast has minutes 9-12 (skipped with under 60 s left; question log
  `round2_skipped`, `followup.skipped`). The existing close-time limits still apply when shorter.
  The last-minute backup chain (Nemotron / Flash-Lite) keeps its own limits. Non-Gemini lineups:
  each forecast at most 12 min.
- **Start cutoff: 30 min** (was 40 in #96). A question whose research turn comes after 30 min into
  the run is not started (public log "not started, the run is past 30 min"); it waits for the next
  run, gets no question log and no retry backoff. The run goes red only if it closes within 25 min.
- **Logs:** each question's log is saved right after its forecast is submitted (or fails), so a job
  killed later still leaves it.
- **Shadows at the end of the run:** the Nemotron shadow forecasts run all together after every
  question is done, only if the run is before minute 45, each within min(240 s, time until minute
  47). Otherwise none run (public log "shadows skipped: time"). Each result goes in its own file
  next to the log, `<log>_shadows.json` (`log`, `shadow_model`, `shadow` = free-shadow and
  live+free-shadow); the Scoreboard merges it into the log (`scoreboard.merge_shadow_files`).
- **Rehearsal** (`timing_rehearsal.py`, Test Bot `timing-rehearsal`; unit test): the real bot code on
  a virtual clock, 0 model calls. A: burst of 5 at every limit (slow research, one hung forecast per
  question): 36.0 min, closing order. B: overload of 12, 11.5-min forecasts: 8 start (last at 28),
  the 4 latest-closing wait, 47.0 min. C: late run (questions arrive at minute 25.5; one starts at
  29.5): 45.4 min, shadows skipped. In each, a job killed at any minute 1-60 leaves every submitted
  question's log saved. (Numbers before the minute-9 rule; now A 33.0, B 44.5 with 8.5-min forecasts,
  C = worst case: one question starts at 29.5, every planned forecast hangs, the quick forecast
  answers at minute 11.9: 45.4 min.) D: every planned forecast hangs, one run per type: the quick
  forecast submits at minute 11.5 of 12, every type. Two such questions in ONE run: the second gets
  no forecast (the cut calls use up each Flash model's 2 failures for the run, #60) and is retried
  by the next run. Worst case: a question starting just before minute 30 ends by 46; with
  shadows the run ends by 47.
- A model call cut by these limits counts as a failed attempt (quota ledger, #60): 2 in a run and
  that model is skipped for the rest of the run.

## Empirical window baseline (1 Oct 2026, shadow only)

`window_baseline.py`, called after submission (`main._attach_hard_data`), replaces the random walk (2b,
end value only) for numeric questions matched to official data. Shadow `window` in the question log,
scored by the Scoreboard, never submitted. Old logs keep their `random-walk` shadow (still scored).
- Data: the series' full daily history (FRED: every observation, fetched only here and not saved in the
  log; CoinGecko's public API: the last 365 days only). Fetch limit 60 s; a failure is only logged.
- Windows: one per past start day, the question's length (open date to resolve date). Windows starting
  within +/-25% of today's value if there are at least 30, else all windows; fewer than 30 in all: none.
- Statistic from the wording (same words as the stand-in rule): END, MAXIMUM or MINIMUM over the
  window; an average gets no baseline. Scaled to today: ratio x today for prices, coins and the VIX;
  change + today for rates and percentages (a ratio near zero would explode).
- 9 percentiles -> PCHIP + 5% uniform -> platform checks, like a model forecast.
- Detail in the private log (`hard_data.window_baseline`: statistic, days, windows, similar or all,
  percentiles, warning). Public log: statistic, days, window count, and for an intraday max/min the
  stand-in warning (daily closes: the true maximum is a little higher).
- 45868 (VIX intraday high, 86 days, real FRED history 1990-2026): 4,997 similar windows; median 21.4,
  10%: 17.1, 90%: 33.1, P(>40) 5.5% (live: 28.7 / 19.9 / above 40 / 19.3%; random walk: 16.4 / 7.1 / 36.9 / 7.6%).
  Raw check: 5.5% of the 4,991 windows starting with the VIX at 12-20 closed above 40 within 86 days.

## Paid-Flash fallback (30 Sep 2026, ON)

`bot_config.PAID_FLASH_FALLBACK` (default False = no spend). When ON, every Flash forecasting chain of the
live gemini-free pool (planned slots, round 2, quick forecast) keeps its free Google Flash models first and
ends with ONE paid attempt on OpenRouter `google/gemini-3.6-flash` (high reasoning, 300 s). It is reached
when the whole free chain can't answer: 503, 429, timeout, or no free quota (the #74 "free first" shape).
Flash-Lite (parser, research, emergency) never gets it. Spend guards (#81, `spend.py`), all fail closed:
- per-question cap $0.15 (`PAID_FLASH_QUESTION_CAP`; each running call reserves the estimate, ~$0.036:
  at most 4 paid calls at once per question), daily cap $1.00 per UTC day (`PAID_FLASH_DAILY_CAP`,
  checked on every call, running calls included), a refused call raises `SpendCapReached` (no guess);
- cost unknown -> charged the estimate (+ alert); failed after it may have been billed -> the estimate;
- each run, before forecasting (`spend.paid_fallback_guard`): ledger `status/spend.json` unreadable or
  MISSING -> no paid call this run; key usage unreadable -> none this run; key spent more than our
  ledger (max(20%, $0.50)) -> none for the rest of the UTC day; each with an alert issue.
- The lineup is marked billed (`free_only` False) only when ON. The OpenRouter key limit is the ceiling.
- Rehearsal: `paid_flash_rehearsal.py` (Test Bot `paid-flash-rehearsal`; unit test).
- **Rules of 2 Oct (architect):** paid Flash only fills a question up to 3 real forecasts in total
  (`PAID_FLASH_TARGET_FORECASTS`: forecasts finished this run + paid calls running; refusal "enough
  forecasts"); the paid slot is only on a question's first 3 planned chains (spare-quota slots are free
  only); per-question cap $0.09; MiniBench may use paid Flash only while the UTC day's paid spend is under
  $0.50 (`PAID_FLASH_MINIBENCH_DAILY_CAP`; unknown tournament = MiniBench), seasonal up to $1.00. The guard
  reserves a measured $0.03 per call (`PAID_FLASH_ESTIMATE`; 1 Oct: 7 calls $0.0227-0.0289, median 6,426
  output tokens), so 3 x $0.03 = the $0.09 cap. High reasoning kept. A free answer that arrives while paid
  calls are already running can still make a 4th forecast (bounded by the cap).
- ON since the switch PR (Tony, 30 Sep): the lineup allows exactly ONE paid model (`Lineup.allowed_paid` =
  `PAID_FLASH_MODEL`); any other paid model fails `get_lineup`'s guard. Each run also reads the key's
  remaining limit: under $2 (`spend.KEY_LOW_DOLLARS`) -> an alert issue (once a UTC day) so Tony can raise
  the limit or let the bot fall back to free models. OpenRouter's docs don't say whether a key that
  reached its limit still serves its `:free` models (Nemotron backup and shadow use the same key).
- **Before switching ON:** create `fall26-data/status/spend.json` as `{}` (missing = blocked by design);
  CLAUDE.md's zero-spend rules ("never call a paid model") must be changed by Tony/the architect.
- OpenRouter docs (https://openrouter.ai/docs/api-reference/limits, 30 Sep): the `:free` daily limit
  (50 or 1,000 requests) "is selected by all-time credits purchased, independently of is_free_tier":
  spending the $10 does not lower it. But "If your account has a negative credit balance, you may see
  402 errors, including for free models."

## Replay lab (29 Sep 2026)

Test-only, never submits (`lab.py`, workflow "Replay lab", started once a Pacific day by the
tournament run in the day's last 3 hours, `health_dispatch.py`; `synthetic` input = end-to-end on a
synthetic resolved question). Live question logs now keep each dossier part with its time:
`dossier_sections` = base, followup, wikipedia, official_data. For resolved questions the lab runs
E1 3 vs 5 forecasts (free when live had 5+), E2 without follow-up, E3 without Wikipedia (3 Gemini
forecasts each), E4 + Nemotron; scores (binary log score, MC log score, numeric log density per unit
of range) and reports paired differences vs live with a bootstrap 90% CI per type
(`lab/results.json`; section in the weekly Scoreboard). Quota: Nemotron <= 300 lab calls/UTC day
(`lab/nemotron_calls.json`); Gemini only in the last 3 h of the Pacific day, only non-reserve quota
beyond 2 x expected questions left, never while MiniBench is open or was in the last 6 h; single-model
chains without the reserve; a 6-minute time budget per run. Leak guard: a model released after the
question opened (OpenRouter `created` date) or of unknown date is never used.

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

## Silent tests / shadow mode (PLAN.md Step 10)

`shadow.py` + `shadow_score.py` (tests: `tests/test_shadow.py`):
- Shadow forecasts for **binary** questions are saved in the question's JSON log (`shadow`) and
  **never submitted** (tested: the live answer is unchanged).
- **Free variants (on):** `stretch-1.2` (the PLAN's stretch setting, off live, so Step 11 gets
  data) and `mean` (mean instead of median); both go through the same final steps. No model calls.
- **Referee (OFF until credits, `shadow.REFEREE_ENABLED`):** sees each forecaster's number plus a
  two-line reason, answers "Probability: ZZ%", is kept between the lowest and highest forecast;
  candidate = average of the referee and the median. Uses the planner's quick forecaster (Opus
  5.5 on credits).
- **Scoring:** see Scoreboard below. PLAN.md: the referee goes live only after ≥80 resolved
  binary questions and only if it beats the median.

## Scoreboard

`scoreboard.py` + workflow **Scoreboard** (Mondays 06:30 UTC + manual; tests
`tests/test_scoreboard.py`; replaces the earlier "Shadow scores" workflow):
- Reads every tournament question log in fall26-data (the latest submitted log per question),
  looks up resolutions with the bot's token, and scores the live forecast: binary ln(p) /
  ln(1−p); multiple choice ln(p of the option that happened); numeric/discrete ln(mass our CDF put
  in the grid bucket the answer fell in, tails for below/above the range). Raw log scores (higher
  = better), not Metaculus peer scores.
- Shadow variants (binary) on the same questions, with the mean difference from live.
- Counts and means overall and per type; saved to `fall26-data/scoreboard/<date>.md` and the job page.

## Market prices (PLAN.md Step 9), LOG-ONLY

`markets.py` (tests: `tests/test_markets.py`). **`bot_config.MARKET_MODE = "off"` since 28 Sep**
(architect: 0 of 20 candidates matched; saves Flash-Lite quota). The code stays; with "log-only":
- For each **binary** question, **after** its forecast is submitted: keywords from the title →
  Polymarket (gamma public-search), Kalshi (v1 search + v2 market for rules and 24 h volume),
  Manifold (search-markets), all keyless. Up to 2 candidates per source, 5 in all.
- Checks: **liquid** (volume ≥ 10,000 Polymarket / 1,000 Kalshi / 1,000 Manifold) and **fresh**
  (price updated within 24 h: Polymarket updatedAt, Manifold lastUpdatedTime, Kalshi 24 h volume).
- **Judge:** the parser model (Flash-Lite live, never the forecasting quota; 1 call per question)
  answers same event / same resolution source / same deadline for each. **Accepted only if all
  three are yes and it's fresh and liquid.**
- Saved as `market_candidates` in the question's JSON log. **Never blended**: the forecast is
  identical with it on or off (tested). A failure is only logged.
- Not done: multiple-choice mapping (every option must map to a market), and blending (Step 9's
  50/50 log-odds blend), both waiting for the architect.
- Manual workflow **Market matches** (`markets.py --limit N`) judges a sample of open main-site
  binary questions with the free OpenRouter model and saves a table (run summary + fall26-data).

**Second free test provider: DROPPED (28 Sep 2026).** Cerebras needs a card; Groq's 8K tokens-per-minute cap blocks one 8k-token prompt; Mistral's own docs say API keys don't work until billing (a card) is active, so it breaks zero spend. Tier C tests stay on the free OpenRouter models, batched after 00:00 UTC. Check "no card" claims on the provider's own docs only.

## Test bench (PLAN.md Step 5)

**DROPPED (28 Sep 2026):** Metaculus shows the community prediction on the website (account artvandelay) but deliberately leaves it out of the API, so the bench has no reference to score against. Code kept, unused; don't try to work around it.

`bench.py` + manual workflow **Test bench** (`evaluate.yml`), never publishes. Tests: `tests/test_bench.py`.
- Picks open main-site questions with a visible community prediction and ≥30 forecasters: quick
  = 30 (18 binary, 3 numeric, 3 discrete, 6 multiple choice), full = 60. The list is saved in
  `fall26-data/bench/<size>/questions.json` and reused.
- Research is gathered once per question (AskNews + free news, no model calls) and frozen in
  `bench/<size>/research/`, so every config sees identical research.
- Each config runs the bot's own forecasting code with research frozen; each result is cached in
  `bench/<size>/results/<label>/`, so a stopped run (e.g. at the OpenRouter free limit) resumes.
- Score: KL(community ‖ ours), lower is better: binary directly; multiple choice over options;
  numeric/discrete over the CDF's buckets (plus below/above the range). Report: mean KL per type;
  for A vs B the paired mean difference (B − A) with a bootstrap 90% CI (2,000 resamples,
  fixed seed), plus cost. Saved in `bench/<size>/reports/` and the run summary.
- Configs: `free` or `credits`; **`gemini-free` is refused** (never the live Gemini quota). The
  noise level = the same config twice under two labels.
- **Community predictions are read with `METACULUS_READ_TOKEN`** (Tony's personal account,
  read-only, added 28 Sep): the bot account can't see them. It's passed **only** to
  `evaluate.yml`, which no longer gets `METACULUS_TOKEN`. The bot forecasts a copy of each
  question with the community prediction removed (`bench.blind`), and `main.py` refuses to start
  if `METACULUS_READ_TOKEN` is set, so the live bot can never use it.
- Sizes: mini = 10 (fits the OpenRouter free limit), quick = 30, full = 60. An empty question
  list is never saved.
- Its own concurrency group, so it never holds up the live bot.

## Reading answers without a model call (item 4a)

`answer_parsing.py` (tests: `tests/test_answer_parsing.py`): the prompts ask for a fixed last-lines
format, so the bot first reads it directly: "Probability: ZZ%" (last one wins), one
"Option: p" line per option (all options, percent or decimal, sum ≈ 1), and all nine
"Percentile P: value" lines (a value may be followed only by the question's unit; "1.2 million"
etc. go to the parser model for unit handling). Only if that fails is the parser model called,
as before. This saves a parser call per forecast: up to 3 per question on the scarce free Gemini
parser quota, and half of each free-model Test Bot run (OpenRouter ~50/day).

## Smart ensemble (PLAN.md Step 6), credits-ready

`ensemble.py` + `bot_config.CreditsPlanner` (tests: `tests/test_ensemble.py`):
- **Round-2 rule (binary):** round 2 runs only if round 1's spread is over 15 points or its
  median is below 10% / above 90%. The final answer is the median of all forecasts, then the
  Step 4 steps. Numeric and multiple choice: everyone at once.
- **gemini-free (live):** round 1 = up to 3 different Gemini models (as before); round 2 = up to
  2 more from models not used in round 1, usable budget only (never the reserve).
- **credits (OFF, `ACTIVE_LINEUP` = "gemini-free"):** the PLAN.md section 3 lineup. Round 1
  Opus 5.5, GPT-5.6 Sol, Gemini 3.6 Flash; round 2 Fable 5.1, Opus 5.5, Gemini 3.6 Flash; numeric
  and MC all 6. Backups: Fable 5.1 → Opus 5.5 → Opus 5; Opus 5.5 → Opus 5; GPT-5.6 Sol → GPT-5.5;
  Gemini 3.6 Flash → Gemini 3.1 Pro (preview). High reasoning, 600 s, 3 tries. Parser, summarizer
  and research planner: Gemini 3.6 Flash.
- **Spending tiers (credits):** target = (remaining − 15% of limit) / expected remaining questions
  (days to 6 Jan 2027 × 12/day, to retune). Full if ≥ $1.75, Standard if ≥ $0.80, else Lean.
  MiniBench one tier below. Chosen once per run from `/api/v1/key`; the last tier is kept in
  `fall26-data/status/spending_tier.json`; a change opens a GitHub issue (GitHub emails Tony).
- Not done: the fast path's own model set (the quick forecast uses Opus 5.5 → Opus 5 on credits).
- **OpenRouter free limit:** the free tier allows about **50 requests a day** across all `:free`
  models (resets 00:00 UTC). A Step 6 Test Bot run on 27 Sep hit it (RateLimitError), so the
  testing-only `free` lineup now researches with AskNews + free news (no model calls), leaving 2
  free calls per test question (forecast + parse). Step 7's planner/dossier passed its own Test
  Bot run (PR #22).

## Research (PLAN.md Step 7)

`research.py` (tests: `tests/test_research.py`), researcher `planned-research` in `bot_config`:
1. **Planner:** the lineup's parser model (on the free key: Gemini 3.5 → 3.1 Flash-Lite → 3.1
   Flash-Lite preview; never the forecasting quota) writes up to 3 search queries and the key
   facts that decide the question (JSON).
2. **Search:** one AskNews call per query (strategy "default", 8 articles), **at most 3 AskNews
   calls per question**, spaced for the free tier's 1 call per 10 s. One call is kept back for the
   gap-fill (the 3rd query is used only if the first two found fewer than 3 articles). If AskNews
   fails (e.g. 402) or finds fewer than 3 articles, Google News RSS + GDELT search the same
   queries (up to 10 articles, deduplicated, newest first).
3. **Dossier:** the parser model writes Current status (with dates) / What must happen to resolve /
   Base rates / Key uncertainties, citing dates, no probabilities. Lines mentioning markets,
   betting odds or crowd forecasts are removed. Capped at ~6,000 tokens (4,500 words).
4. **Gap-fill:** if the dossier's last line names a missing fact, one extra search (AskNews if a
   call is left, else free news) is appended.
- If the planner or dossier writer fails, the articles themselves are the research.
- Not done: the model web search via OpenRouter (Step 2 not reached; grounding isn't free), and
  the "round-1 models disagree by >15 points" gap-fill trigger (needs Step 6's rounds).
- Log: `articles found: N (AskNews a in c call(s), free news f); q queries; dossier ...; words`.
  The question JSON log keeps the queries and counts (`research_detail`) and the dossier text.

## Numeric and multiple choice (PLAN.md Step 8)

`distributions.py`, tests with worked examples in `tests/test_distributions.py`:
- **Numeric/discrete:** the prompt asks for percentiles 2.5, 5, 10, 25, 50, 75, 90, 95, 97.5.
  Each model's CDF is built with **PCHIP** (monotone, no overshoot; own numpy implementation,
  no scipy) through those points, in the question's own x-axis, so log-scaled questions work.
  Closed bounds pin the CDF to 0/1; open bounds extend the tails in line. The library's
  standardising then makes it platform-valid.
- **Combining:** pointwise median of the models' declared CDFs, then
  `final(x) = 0.95 × median + 0.05 × location(x)` (uniform over the range, in the question's
  x-axis), then the Step 4 checks. Metaculus' own standardising (0.99·F + 0.01·location for
  closed bounds, slightly different for open ones) is applied once more when submitting.
  Combining on the declared CDFs (not re-standardised copies) avoids stacking that 1% blur
  several times.
- **Multiple choice:** median per option → renormalise → every option at least 1% → the extra
  comes from the other options in proportion (`floor_probabilities`). Note: the library itself
  clamps options to 1% and renormalises whenever an option list is built, which leaves them just
  under 1% (0.99%); we floor on plain numbers first to avoid that.

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
  re-parsed once with a units warning; if still wrong, that forecast is dropped.
- **Unit check vs the current value (28 Sep):** for numeric/discrete questions the Step 7 dossier
  ends with `CURRENT VALUE: <number> | UNIT: <unit> | DATE: <date>` (or `unknown`), parsed into
  `research_detail.current_value`. If a model's median is more than 10× or less than 0.1× that
  value (positive values only; skipped if the value is missing or ≤ 0), the parser is re-asked
  once with a units warning naming the current value. If still off, that model is **dropped** when
  combining, unless **every** model is flagged: then all are kept (the current value is probably
  wrong) and a warning is logged. Never a made-up forecast (architect, 28 Sep: no wide
  distribution around the current value). Tests: `tests/test_unit_check.py` (x1000 and x11 examples). Not done yet: the
  "median more than 10x or under 0.1x the current value from research" check, because the
  research has no structured current value yet (planner facts, Step 7).
- **Every prompt** includes today's date and the question's close and resolve dates.
- **Before submitting,** the bot re-fetches the question and submits only if it's still open and
  unresolved. The library's own publishing is off; the bot submits itself. If the re-check itself
  fails, it submits anyway rather than risk skipping.
- **Never a pure guess** (architect, 27 Sep; PLAN.md rule 6): no 50% / equal-odds / flat
  fallback forecasts on real questions. An invalid forecast is dropped; if at least one real
  forecast is left, the median is submitted. If none, nothing is submitted and the question is
  retried by the next run (it isn't marked as forecast) until it closes. A tournament run with
  such a question is only red if the question closes within 25 minutes (a later run might not
  get to it); otherwise it leaves a `question-retry` warning. The daily health check goes red
  for any question that closed in the last 24 h without our forecast.

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
| Keepalive | `keepalive.yaml` | schedule daily 05:00 UTC; manual | No: calls GitHub's "enable workflow" API for every workflow (no commit; since 1 Oct main requires the "Validate workflows" check, so a direct push is rejected) |
| Workflow lint | `workflow_lint.yml` | every pull request | No: YAML parse + actionlint (with shellcheck); job "Validate workflows" is a required check on main (ruleset "main: workflows must validate") |

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
