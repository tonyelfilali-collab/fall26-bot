# Fall 2026 Metaculus Bot: Master Plan

Version 1.1, 27 Sep 2026. Repo: https://github.com/tonyelfilali-collab/fall26-bot

---

## 0. How to use this document

**Who does what**
- **Tony (owner):** adds keys, relays messages. Never edits code or forecasts, and doesn't merge.
- **Architect:** a Claude chat in the Claude project. Plans, reviews each pull request, decides.
- **Builder:** Claude Code, working in the repo above. Opens pull requests, and merges them **only after the architect says "merge"** (Tony relays it).

**Starting a new chat with the architect:** open a new chat inside the Claude project and send:
> Read PLAN.md in the project files. We are at Step __. Here is what happened since: ...

**Keeping it up to date:** when the plan changes, the architect gives Tony a new PLAN.md. Tony replaces it in two places: the Claude project files, and the repo (Claude Code does the repo part).

**Progress log:** at the very bottom. Update it after every step.

---

## 1. Goal and hard rules

**Goal:** win as much prize money as possible in the Metaculus Fall 2026 FutureEval bot tournament and the MiniBench rounds.
- Seasonal tournament: slug `fall-futureeval-2026`, id 33121, $50k prize pool. Questions open from 28 Sep 2026 to about 6 Jan 2027; the tournament page's close date (5 Mar 2027) is when the last questions resolve.
- MiniBench: $1k per 2-week round. The slug `minibench` pointed to tournament 33125 (21 Sep–9 Oct 2026). Still to check: whether `minibench` moves to the next round automatically.

**Hard rules**
1. **Zero spend.** Allowed:
   - Metaculus's donated OpenRouter credits (OpenAI, Anthropic and Google models only)
   - AskNews free tier
   - free public APIs (Polymarket, Kalshi, Manifold, FRED, Wikipedia)
   - free GitHub, healthchecks.io free plan

   Not allowed: no card on OpenRouter, no Perplexity, no paid APIs.
2. **No human in the loop.** Nobody edits, previews or submits a forecast by hand.
3. **The bot runs only on GitHub Actions.** The code repo is public; the data repo is private.
4. **One small pull request per task.** Claude Code merges it only when checks are green and the architect has said "merge". Notes-only changes (NOTES.md, the progress log) may be merged without waiting.
5. **Every change that affects forecasts is tested before it goes live** (see section 6).
6. **A missed question scores 0, and a pure guess scores below 0 on average.** Reliability always comes before cleverness. Never submit a pure guess (50% binary, equal odds, flat numeric) on a real question. With at least one real model forecast, submit it (the median of what's finished). With none, leave the question for the next run and keep retrying until it closes. Only if nothing works by the close is it skipped and logged (health.yml flags it).

---

## 2. Key facts (checked 27 Sep 2026)

**Tournament**
- Question types: binary, numeric, discrete, multiple choice.
- Up to 5 questions are released at once, at random times. Each seasonal question is open about 1.5 hours.
- Only the last forecast before close counts (spot peer score, log-based).
- The first 1–2 weeks have fewer questions.

**Template and library**
- The bot is built on the official metac-bot-template with forecasting-tools 0.3.1, the first version with Fall 2026 constants (id 33121).
- By default the library combines forecasts like this: binary = median; multiple choice = mean; numeric/discrete = median of the CDFs.

**Secret names the code reads:** `METACULUS_TOKEN`, `OPENROUTER_API_KEY`, `ASKNEWS_API_KEY` (a single AskNews key; the older two-part ID/secret isn't used).

**Confirmed by Claude Code:** each question's forecast is submitted as soon as that question is done; AskNews uses 2 calls per question; the personal OpenRouter key has no limit but $0 balance, and the bot refuses paid models while the free setup is on.

**Model leaderboard** (FutureEval, single model + AskNews, 27 Sep 2026):

| Forecaster | Score |
|---|---|
| Metaculus Community | 25.21 |
| Claude Fable 5 High | 16.10 |
| Claude Opus 5 High | 15.78 |
| Claude Opus 4.8 High | 14.13 |
| Gemini 3.6 Flash | 13.23 |
| GPT-5.6 Sol High | 12.93 |

Opus 5.5 and Fable 5.1 are too new to appear yet.

**AskNews free tier:** 1,000 calls a month, 4,000 for the whole tournament. With about 800 questions, the bot must use **3 calls or fewer per question**.

**GitHub:** in a public repo, scheduled workflows pause after 60 days with no commits, so the bot needs a weekly keepalive commit.

**Still unknown (waiting on answers)**
- [ ] How much credit we get, and which models it covers (asked Ben at Metaculus)
- [ ] Whether donated credits may be used for MiniBench, the testing area and the test bench
- [ ] Prize formula; whether Ramp pays to a UK bank account
- [ ] Whether the bot may use the community prediction of a main-site "twin" question
- [ ] Whether manually re-running a failed workflow is allowed
- [ ] How long MiniBench questions stay open (rerun Tournament info when some are open)
- [ ] Whether the free Google AI Studio key works with Gemini 3.6 Flash (Plan B, below)
- [ ] Whether OpenRouter web search works on the donated key

---

## 3. How the finished bot works

1. **Find questions.** Every 10 minutes, fetch open Fall and MiniBench questions we haven't forecast yet.
2. **Plan.** Check the remaining credit and pick a spending tier. If the question closes within 25 minutes, use the fast path.
3. **Research.**
   - A planner model writes search queries and lists the facts that decide the question.
   - The bot searches AskNews and uses model web search.
   - It does an extra search only if key facts are missing or the models disagree.
   - The result is a short dossier (6k tokens or less) with dated facts, the current value, base rates and key uncertainties.
   - Market prices are kept **out** of the dossier.
4. **Forecast.** Several models from different AI families each forecast independently, at high reasoning effort.
5. **Combine and adjust** in a fixed order (below).
6. **Check.** The forecast must pass the platform rules and sanity checks. A forecast that fails them is dropped, never replaced by a guess. If at least one real forecast passes, submit the median of those. If none does, leave the question for the next run (every 10 minutes) until it closes; if nothing works by then it's skipped and health.yml flags it.
7. **Submit** the forecast with a private comment, as soon as that question is ready.
8. **Record.** Save everything to the private data repo and ping the "bot is alive" monitor.

### Model lineup

- **Yes/no questions**
  - Round 1: Claude Opus 5.5, GPT-5.6 Sol and Gemini 3.6 Flash.
  - Round 2 runs only if the round-1 spread is more than 15 points, or the median is below 10% or above 90%. It adds Claude Fable 5.1, Claude Opus 5.5 and Gemini 3.6 Flash.
- **Numeric and multiple-choice questions:** always all 6 forecasts, because that's where bots lose the most points.
- **Planner, parser and summarizer:** Gemini 3.6 Flash.
- **All forecasters:** high reasoning effort, 600-second timeout, 3 tries.
- **Backups** when a model fails, times out or refuses:
  - Fable 5.1 → Opus 5.5 → Opus 5
  - Opus 5.5 → Opus 5
  - GPT-5.6 Sol → GPT-5.5 High
  - Gemini 3.6 Flash → Gemini 3.1 Pro

### Adjusting the final number (always in this order)

1. **Middle value:** take the median of all the forecasts.
2. **Stretch** (yes/no only): push the median slightly away from 50%. In log-odds, multiply by k = 1.2 (retuned in Step 11). Never stretch a market blend or a referee answer.
3. **Market blend** (Step 9 onwards): if there's a good market match, the final number is 50% ours and 50% the market's, blended in log-odds.
4. **Extreme check:** a forecast below 5% or above 95% is only allowed if at least 80% of the forecasts that ran are beyond 10%/90% on the same side. If only 3 models ran and the median is extreme, run all 6 first. Otherwise, pull the forecast back to 10%/90%.
5. **Clip:** yes/no forecasts are always kept between 2% and 98%.

For multiple choice: take the median per option, rescale so they sum to 1, give every option at least 1%, then rescale again.

For numeric: take the pointwise median of the models' CDFs, mix 95% of it with 5% uniform spread across the question's range, then check the platform's CDF rules.

### Spending tiers (chosen automatically)

The bot works out a target spend per question: (remaining credit − 15% reserve) ÷ expected remaining questions. It then picks a tier:

| Tier | Models | Rough cost per question |
|---|---|---|
| Full | The lineup above | ~$1.50–2.00 |
| Standard | Opus 5.5, GPT-5.6 Sol, Gemini 3.6 Flash ×3 | ~$0.80 |
| Lean | Opus 5.5 + Gemini 3.6 Flash ×2 | ~$0.40 |
| Fast path (less than 15–25 min to close, or the full path failed) | Opus 5.5 + Gemini 3.6 Flash ×2, AskNews research only | ~5 minutes to run |

- MiniBench always runs one tier below the seasonal tournament.
- Tony gets an email whenever the tier changes.
- All costs are estimates; the job summary logs the real cost.

---

## 4. Build steps

**How every step works**
1. Tony pastes the "Message for Claude Code" into Claude Code.
2. Claude Code builds it and opens a pull request.
3. Tony sends the architect Claude Code's reply and the pull request link.
4. The architect checks it and says "merge" or explains what to fix.
5. Tony tells Claude Code "The architect says merge". Claude Code merges it and adds a row to the progress log.

Do steps back to back, as fast as each one passes its check. Only Step 2 (credit key) and Step 11 (needs resolved questions) have to wait for something outside our control.

---

### Step 1: Foundation (Tasks 1–4) plus three checks

**Status: DONE (27 Sep).** PRs #1–#6 merged; Test Bot green on all 4 types at $0; tournament workflow on but exits while `BOT_ENABLED` is off; workflows pinned to ubuntu-24.04. What was built:
- Task 2: target Fall 2026 explicitly.
- Task 3: all model choices in one config file, free test models for now, cost per question in the run summary.
- Task 4: safety — failed runs show red, a shared concurrency group, 60-minute timeout, a second schedule at minutes 17/37/57, a weekly keepalive, no forecasts or reasoning in public logs, a `BOT_ENABLED` switch, and a Credit check workflow.

**Message for Claude Code (after Task 4):**
```
Three checks before we go further. Don't change behaviour; just find out and add the answers to NOTES.md with sources:
1. Does forecasting-tools submit each question's forecast as soon as that question finishes, or all at the end of the run? If at the end, tell me what it would take to submit per question.
2. How long do current Fall MiniBench questions stay open? Confirm the Fall MiniBench slug/id via the Metaculus API in an Actions run, and report open and close times for a few questions.
3. How many AskNews API calls does 'asknews/news-summaries' make per question? We must stay at or below 3 per question.
Reply in plain, short language.
```

**Done when:**
- Test Bot is green on the testing area with a free model, for all 4 question types.
- The tournament run logs "BOT_ENABLED is not true, exiting".
- Credit check works.
- The three answers are in NOTES.md.

---

### Plan B: free Gemini key (in case credits are slow or never come)

The credits form warns that many bots will be left unfunded this season. Google AI Studio gives a free API key (no card) for Gemini models, and Gemini 3.6 Flash scores 13.2 on the leaderboard, not far behind the top models. Plan B runs the bot on that key until (or instead of) Metaculus credits.

**Tony:** go to aistudio.google.com → sign in with a Google account → **Get API key** → **Create API key** → copy it → add it as repository secret `GEMINI_API_KEY`. Never add billing.

**Message for Claude Code:**
```
Plan B (free Gemini key). GEMINI_API_KEY is now a repository secret (Google AI Studio free tier, no billing). Follow CLAUDE.md and PLAN.md.
1. Check which Gemini models this key can use for free and their rate limits. We want Gemini 3.6 Flash; report what's available.
2. Add a 'gemini-free' setup to bot_config.py: 5 forecasts per question from Gemini 3.6 Flash (high reasoning if supported), parser Gemini 3.6 Flash, AskNews research. Throttle and retry so the free rate limits are never exceeded; if a limit is hit near a question's close, submit what's ready.
3. The free-only guard must allow this key (it can't be charged) and still block every paid OpenRouter model.
Done when: Test Bot is green on all 4 question types using the Gemini key at $0, and the run page shows no rate-limit failures.
```

**Then:** when the architect says so, Tony sets `BOT_ENABLED` = `true` and the bot starts forecasting real questions on Gemini. When the Metaculus key arrives, Step 2 switches it to the full lineup.

---

### Step 2: Switch on (when the Metaculus credit key arrives)

**Tony:**
1. Update the `OPENROUTER_API_KEY` secret with the key Metaculus sends.
2. Run **Credit check** and tell the architect the limit it shows.

**Message for Claude Code:**
```
Step 2 (Switch on). The donated Metaculus key is now in OPENROUTER_API_KEY. Follow CLAUDE.md and PLAN.md.
1. Production lineup in the config file: 5 forecasts per question from different models: Claude Opus 5.5, GPT-5.6 Sol, Gemini 3.6 Flash, Claude Fable 5.1, Claude Opus 5.5. All high reasoning, timeout 600 s, 3 tries. Parser and summarizer: Gemini 3.6 Flash.
2. Backups when a model errors, refuses or times out: Fable 5.1 -> Opus 5.5 -> Opus 5; Opus 5.5 -> Opus 5; GPT-5.6 Sol -> GPT-5.5 High; Gemini 3.6 Flash -> Gemini 3.1 Pro.
3. AskNews: at most 3 calls per question.
4. Test OpenRouter web search once with this key. Report whether it works and whether it's charged to the donated credits.
Done when: Test Bot is green on all 4 question types; the job summary shows each model id, reasoning tokens > 0, and cost per question.
```

**Then Tony:** Settings → Secrets and variables → Actions → **Variables** tab → New repository variable, name `BOT_ENABLED`, value `true`. The bot is now live.

---

### Step 3: Never miss a question

**Tony first:**
1. **Dead-man's switch.** Sign up at healthchecks.io (free) → Add Check → Period 1 hour, Grace 1 hour → copy the ping URL → add it as repository secret `HEALTHCHECK_URL`.
2. **Private data repo.** Create a new private repository named `fall26-data`.
3. **Data token.** GitHub → Settings → Developer settings → Fine-grained tokens → Generate new token:
   - Repository access: only `fall26-data`
   - Permissions: Contents = Read and write
   - Expiration: 31 Jan 2027

   Copy it and add it as repository secret `DATA_REPO_TOKEN` in fall26-bot.

**Message for Claude Code:**
```
Step 3 (Never miss). Follow CLAUDE.md and PLAN.md.
a) Submit each question's forecast as soon as it is ready (not at the end of the run).
b) Deadline rule: if a question closes within 15 minutes and the full forecast isn't finished, submit the median of the forecasts made so far; if there are none, run the fast path (Opus 5.5 + Gemini 3.6 Flash x2, AskNews research only).
c) Any failure in the full path falls back to the fast path. Never submit a pure guess: submit a real model forecast (the median of what's finished), or leave the question for the next run and retry until it closes.
d) Ping HEALTHCHECK_URL at the end of every tournament run (append /fail if the run failed).
e) New daily workflow health.yml at 07:00 UK time. It fails red if: an open question closes within 60 min without our forecast; credits are below 25%; there has been no successful run for 3 hours; AskNews quota is below 20%.
f) Save one JSON file per question to the private repo fall26-data (using DATA_REPO_TOKEN): question snapshot, research with timestamp, each model's raw and parsed output, final forecast, cost, timings. A logging failure must never block a forecast, but it must trigger an alert.
Done when: a forced model failure still submits a fast-path forecast in the testing area; JSON files appear in fall26-data; health.yml goes red on a simulated miss.
```

**Tony's check:** pause the tournament workflow for 2 hours (Actions → the workflow → "..." → Disable). An email from healthchecks.io should arrive. Then re-enable it.

---

### Step 4: Safety checks

**Message for Claude Code:**
```
Step 4 (Safety checks). Follow CLAUDE.md and PLAN.md section 3.
Build these as small pure functions with unit tests, plus a CI workflow that runs the tests on every pull request:
- Before submitting: the question is still open and unresolved.
- Every prompt includes today's date and the question's close and resolve dates.
- Binary: the fixed 5-step adjustment order in PLAN.md section 3 (median, stretch k=1.2, [market blend placeholder], extreme check using "80% of forecasts that ran", clip 2%-98%).
- Multiple choice: every option at least 1%, sums to 1.
- Numeric/discrete: CDF passes the platform rules (201 points for continuous, increasing, each step no bigger than 0.2, bounds respected).
- Unit sanity: if every percentile is outside the question range, or the median is more than 10x or less than 0.1x the current value found in research, re-parse once; if still wrong, drop that forecast.
- Anything invalid is dropped, never replaced by a pure guess: submit the median of the real forecasts left, or retry the question on the next run until it closes.
Done when: CI runs at least 40 tests, including known disasters (99% on an unresolved question, a x1000 unit error, reversed percentiles), and all pass; Test Bot is green.
```

---

### Step 5: Test bench

**Message for Claude Code:**
```
Step 5 (Test bench). Follow CLAUDE.md and PLAN.md section 6.
New manual workflow evaluate.yml that never publishes:
- Picks 60 open main-site Metaculus questions with a visible community prediction and at least 30 forecasters (about 60% binary, 20% numeric/discrete, 20% multiple choice). Saves the list so we reuse it.
- Gathers research once per batch and freezes it in fall26-data, so config A and config B see identical research.
- Runs config A and config B; scores each forecast's distance from the community prediction (KL divergence); reports the paired mean difference with a bootstrap 90% confidence interval, overall and per question type, plus cost.
- A "quick" option that uses 30 questions.
Done when: a report exists for the current config, and running it twice shows the noise level.
```

---

### Step 6: Smart ensemble

**Message for Claude Code:**
```
Step 6 (Smart ensemble). Follow CLAUDE.md and PLAN.md section 3 ("Model lineup" and "Spending tiers").
- Binary: round 1 = Opus 5.5, GPT-5.6 Sol, Gemini 3.6 Flash. Round 2 (Fable 5.1, Opus 5.5, Gemini 3.6 Flash) only if the round-1 spread > 15 points or the median is below 10% or above 90%. Final = median of all.
- Numeric and multiple choice: always all 6.
- Spending tiers Full/Standard/Lean chosen automatically from (remaining credit - 15% reserve) / expected remaining questions. MiniBench always one tier below the seasonal tournament. Email alert when the tier changes.
Done when: the test bench shows it's not worse (PLAN.md section 6); cost per question is within the expected band; a forced provider failure still produces a forecast.
```

---

### Step 7: Better research

**Message for Claude Code:**
```
Step 7 (Research). Follow CLAUDE.md and PLAN.md section 3.
- A planner (Gemini 3.6 Flash) writes up to 3 search queries and lists the facts that decide the question.
- Sources: AskNews (at most 3 calls per question) plus one model web search via OpenRouter, if Step 2 confirmed it works on the donated key.
- Gap-fill: one extra search only if a key fact is missing or the round-1 models disagree by more than 15 points.
- Dossier: 6k tokens max, containing current status/value with dates, what must happen to resolve, base rates, key uncertainties. No market prices in the dossier. Saved with a timestamp in fall26-data.
Done when: the test bench shows it's not worse; every eval dossier is 6k tokens or less; logs confirm 3 or fewer AskNews calls per question.
```

---

### Step 8: Numeric and multiple-choice handling

**Message for Claude Code:**
```
Step 8 (Numeric and multiple choice). Follow CLAUDE.md and PLAN.md section 3.
- Numeric/discrete: ask each model for percentiles 2.5, 5, 10, 25, 50, 75, 90, 95, 97.5. Build each model's CDF with PCHIP interpolation; take the pointwise median across models; mix 95% with 5% uniform over the question range (respecting open/closed bounds); must pass the Step 4 checks.
- Multiple choice: median per option across models, renormalise, floor every option at 1%, renormalise again.
Done when: all checks pass on the testing area; the test bench shows the numeric and multiple-choice subsets are not worse.
```

---

### Step 9: Market blend

**Message for Claude Code:**
```
Step 9 (Market blend). Follow CLAUDE.md and PLAN.md section 3.
- Search Polymarket, Kalshi and Manifold by keywords from the question.
- A judge model (Gemini 3.6 Flash) accepts a market only if it is the same event, with the same resolution source and exactly the same deadline.
- Require a minimum liquidity/volume and a price updated within the last 24 hours.
- Binary: final = 50% ours + 50% market, in log-odds, applied after the stretch and before the extreme check.
- Multiple choice: only if every option maps to a market.
- Log a table of every candidate match and the judge's verdict.
Done when: a table of 20 candidate matches is sent to the architect for review; the test bench shows it's not worse.
```

---

### Step 10: Silent tests (shadow mode)

**Message for Claude Code:**
```
Step 10 (Silent tests). Follow CLAUDE.md and PLAN.md.
- Build a "shadow variant" system: extra forecasts are computed and saved to fall26-data, but never submitted.
- First shadow variant: the referee, binary only. It sees each model's number plus a two-line reason (not full transcripts). Its answer must lie between the lowest and highest model forecast. Candidate final = average of referee and median.
- A script that scores shadow vs live forecasts on resolved questions (log score).
Done when: shadow forecasts are being saved; the scoring script works on the first resolved questions.
```

**Architect decision:** after at least 80 resolved binary questions, the referee goes live only if it beats the plain median.

---

### Step 11: Tuning (from about mid-November)

**Message for Claude Code** (send once at least 150 binary questions have resolved):
```
Step 11 (Tuning). Using resolved questions in fall26-data, fit the stretch factor k and the clip limits by cross-validation, separately per question type where there's enough data. Report the log score before and after. Change the live settings only if cross-validation shows an improvement.
```

---

### Later (only after Steps 1–11 are solid)

1. **Model upgrades.** Check the FutureEval leaderboard monthly; test any new top model on the test bench and swap it in if it wins.
2. **Monthly review of the biggest misses**, using the forecasting-tools review tool, to catch bugs.
3. **Consistency between related questions** released together.
4. **Market re-check near close**, only if MiniBench windows turn out to be long.
5. **Community prediction of the main-site twin question**, only if Metaculus confirms it's allowed.
6. **Metaculus Cup** on the Lean tier.
7. **Market Pulse** (needs continuous updating), last.

### Don't build

- persona prompts
- forced Bayesian or step-by-step reasoning templates (research says they hurt)
- best-of-k pickers
- stacking models or meta-models
- fine-tuning our own model
- backtests using date-filtered web search (they leak the answer)
- dashboards
- more than about 8 forecasters per question
- anything that runs on Tony's computer
- Grok or other non-covered models (not covered by the credits)

---

## 5. Tony's how-to

- **Merging:** Tony doesn't merge. When the architect says "merge", tell Claude Code: "The architect says merge PR #__."
- **Undo a merged pull request:** tell Claude Code: "Revert PR #__ and merge the revert."
- **Run a workflow:** **Actions** tab → click the workflow name on the left → **Run workflow** (right side) → green **Run workflow** button. Wait a few minutes and refresh.
  - Green tick = it worked.
  - Red X = it failed: screenshot it and send it to the architect.
- **Update a secret:** Settings → Secrets and variables → Actions → click the secret → paste the new value → **Update secret**.
- **Add or change a variable:** same page → **Variables** tab.
- **See the bot's forecasts:** Metaculus → your bot's profile page.
- **Emergency stop:** set the `BOT_ENABLED` variable to `false`.
- **Calendar reminders:**
  - 6 Jan 2027: post-season survey (required for prizes).
  - Early Jan 2027: participation form for the next season.

---

## 6. Test bench and acceptance rule

**Three ways we measure**
1. **Test bench** (Step 5): distance from the Metaculus community prediction on open questions. It's fast and can't leak answers.
2. **Real results:** live and shadow forecasts scored once questions resolve. MiniBench gives about 60 resolutions every 2 weeks.
3. **Safety tests:** unit tests plus a Test Bot run on every pull request.

**When a change ships**

| Kind of change | Examples | Ships when |
|---|---|---|
| Fixes and reliability | bug fixes, alerts, checks | Tests and Test Bot are green |
| Methods already backed by research | ensemble, research breadth, numeric/MC handling | Not measurably worse: the 90% CI of (new − old) is no worse than +2% of baseline, no question type is more than 10% worse, cost stays within tier |
| New or risky ideas | referee, market weight, stretch factor, prompt changes | The 90% CI shows a real improvement, or real resolved results show it wins |

**Always**
- One change per pull request.
- Differences smaller than the noise level count as zero.
- Never merge on a day when nobody can check the next run.

---

## 7. Progress log

| Date | What happened | Next |
|---|---|---|
| 27 Sep 2026 | Accounts created; credits form submitted; email to Ben sent. Step 1 done: PRs #1–#6 merged, Test Bot green at $0, tournament workflow exits while BOT_ENABLED is off. First Fall question (45516) opened; bot is off, so it's skipped. | Plan B (free Gemini key); wait for credit key (Step 2); then Step 3 |
| 27 Sep 2026 (evening) | Plan B live on the free Gemini key (PRs #8, #9): `BOT_ENABLED` = true at 18:00 UTC. First key was on a paid tier and was replaced; free tier = 20 requests/day per model. Pool: 3.6/3.7/3.8/3.5 Flash forecast (up to 3 per question, per-model daily ledger in fall26-data, 20% reserve); parser 3.5/3.1 Flash-Lite (2.5 is closed to new projects). Question 45516 forecast at 18:02 UTC (3 forecasts, all 3.8 Flash because the others were overloaded). AskNews answers 402 (free allowance used up; Tony emailed AskNews); the bot forecasts without news. Grounding with Google Search is not available on the free key. PR #10 (interim limits: binary 3%–97%, MC 1% floor) waiting for the architect. | Architect: review PR #10; decide research source; AskNews reply |
| 27 Sep 2026 (night) | PR #10 merged (interim limits: binary 3%–97%, multiple choice 1% floor; Search grounding not available on the free key). PR #12 merged: free news fallback. When AskNews fails (402) or finds <3 articles, Google News RSS + GDELT supply up to 10 dated articles (last 60 days, ≤2,000 words). Test Bot: "articles found: 10". Log shows counts only. | AskNews reply; watch live runs; Step 2 when the credit key arrives |
| 27 Sep 2026 (night, later) | Found that GitHub never runs scheduled workflows in a fork: the tournament schedule had never fired. Tony detached fall26-bot from the Metaculus fork network (architect approved); secrets and `BOT_ENABLED` survived. Until the schedule is proven, Claude Code starts the tournament workflow by hand once an hour (first 18:30 UTC). | Confirm the first scheduled run; then stop the hourly manual runs |
| 27 Sep 2026 (night) | Step 3 (Never miss) merged, PR #15: 15-minute cut-off with median of finished forecasts; quick forecast (up to 3 passes) if none finished; one JSON file per question in fall26-data; healthchecks.io ping each tournament run; daily health.yml at 07:00 UK. Proofs: health red on a simulated miss; forced failure still submitted; JSON files present; 67 unit tests green. AskNews answered again (16 articles) in the last test. Scheduled runs still not seen; hourly manual runs continue. | Confirm first scheduled run; then stop hourly runs |
| 27 Sep 2026 (late night) | cron-job.org starts the tournament workflow every 10 min (runs 19:52, 20:02, 20:12 UTC confirmed); hourly manual runs stopped. GitHub's own schedule never fired after detaching, but is kept. Step 4 (Safety checks) merged, PR #18: binary 5-step order (stretch k=1.0 off, clip 2%–98%), MC 1% floor, platform CDF rules, reversed percentiles fixed, unit-error re-parse, dates in every prompt, re-check still open before submitting. Architect decisions in the same PR: success-only Gemini quota counting (today's ledger reset to 3.6 Flash only); never a pure guess (rule 6 rewritten); Test Bot on free OpenRouter models only. Test Bot green on all 4 types; 101 unit tests. PR #17 (quota check = warning) waiting for approval. | Architect: PR #17; watch tonight's first seasonal questions (00:00 UTC) |
| 27 Sep 2026 (late night, 2) | PR #17 merged: low Gemini quota is a health-check warning; "no successful run for 3 hours" stays red. Long autonomous build started (items 0–6). Item 0: Steps 3 and 4 wording updated to "real forecast or retry, never a pure guess". | Item 1: Step 8 |
| 27 Sep 2026 (late night, 3) | Item 1: Step 8 (numeric and multiple choice). Numeric: 9 percentiles (2.5–97.5), PCHIP CDF per model, pointwise median, 95% + 5% uniform, Step 4 checks. Multiple choice: median per option, renormalise, 1% floor (proportional). Worked-example unit tests. | Item 2: Step 7 |
| 27 Sep 2026 (late night, 4) | Item 1 (Step 8) merged, PR #21; next cron run green. Item 2: Step 7 research: planner and dossier writer on the Flash-Lite parser models (a 3rd, 3.1 Flash-Lite preview, added for capacity); AskNews max 3 calls/question with one kept for the gap-fill; free news when AskNews fails or finds <3; dossier ≤6k tokens, no market prices. | Item 3: Step 6 |
| 27 Sep 2026 (late night, 4a) | Item 2's next cron run green. Extra small PR (4a): read the forecasters' answers directly ("Probability: ZZ%", option lines, percentile lines) before calling the parser model; fallback unchanged. Saves up to 3 Gemini parser calls per question and makes the free-model test bench fit the OpenRouter free limit (~50/day). | Test Bot after the 00:00 UTC free-limit reset |
| 27 Sep 2026 (late night, 6) | Item 4: Step 5 test bench (bench.py, manual "Test bench" workflow): 30/60 open main-site questions with a community prediction, frozen research, cached/resumable results, KL vs community, paired bootstrap 90% CI per type, cost; configs free/credits only (never live Gemini). | Quick 30-question proof run after merge |
| 27 Sep 2026 (late night, 7) | Item 5: Step 9 in LOG-ONLY mode: after a binary question is submitted, Polymarket/Kalshi/Manifold candidates are found, checked (liquidity, 24 h freshness) and judged (same event / source / deadline) by the parser model, and saved in the question log. Never blended. Manual "Market matches" workflow for the 20-row table. | Item 6: Step 10 |
| 27 Sep 2026 (late night, 8) | Item 6: Step 10 shadow system: binary shadow forecasts saved in the question log, never submitted; free variants stretch-1.2 and mean on; referee built but OFF until credits; manual "Shadow scores" workflow (log score vs live on resolved questions). | Merge queue after the 00:00 UTC free-limit reset; REPORT.md |
| 28 Sep 2026 (00:00–01:10 UTC) | After the OpenRouter free-limit reset: merged Step 6 (#23), answer reading (#24), test bench (#25) + fix (#28), markets log-only (#26), shadow system (#27); every next cron run green; 27/27 live runs green. Test bench quick proof stopped after 2 failures (bot account sees no community predictions). Market matches: 20 candidates, 0 accepted. See REPORT.md. | Architect: REPORT.md questions |
| 27 Sep 2026 (late night, 5) | Item 2 (Step 7) merged, PR #22. Item 3: Step 6 smart ensemble, credits-ready: round 2 only when round 1 disagrees (>15 pts) or is extreme; gemini-free gets up to 2 extra models in round 2; the 'credits' lineup (section 3, with backups and Full/Standard/Lean tiers, MiniBench one tier below, tier change → GitHub issue) is built but OFF. | Item 4: Step 5 |
