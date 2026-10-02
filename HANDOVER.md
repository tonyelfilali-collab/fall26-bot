# HANDOVER.md v3: for a fresh architect session

Version 3, written 2 Oct 2026 (evening UTC). Replaces v2 (28 Sep evening). Covers everything since then.
Read with: `CLAUDE.md` (rules), `PLAN.md` (plan + progress log at the bottom), `NOTES.md` ("What's live"
table at the top), `REPORT.md` (reports), `claude/ARCHITECT_LOG.md` (decisions per session),
`CC_HANDOVER.md` (the Claude Code side).

## 1. Who does what

- **Tony (owner):** adds keys and secrets, relays messages. Never edits code, merges or forecasts by hand.
- **Architect:** a Claude chat in the Claude project. Plans, decides, reviews.
- **Claude Code (builder):** works in `tonyelfilali-collab/fall26-bot`, opens one small PR per task and
  merges it itself by tier (CLAUDE.md "Merging"):
  - **Tier A** (notes, health, CI, tools the live run doesn't use): unit tests green -> merge.
  - **Tier B** (forecast plumbing, no prompt/model change): + replay Test Bot on 4 types -> merge.
  - **Tier C** (prompts, models, answer reading, dossier): + real free-model Test Bot on 4 types (just
    after 00:00 UTC) -> report; merge only after "the architect says merge".
  - Always: check the next live run after a merge; revert if red; never two forecast-path merges
    between two live runs; a progress-log row per merge.
- Since 1 Oct every PR must pass the required check **"Validate workflows"** (section 8).

## 2. Goal and hard rules

- Goal: most prize money in the Fall 2026 FutureEval tournament (`fall-futureeval-2026`, id 33121) and
  the MiniBench rounds.
- **Zero spend, one exception (Tony, 30 Sep):** never a paid model EXCEPT `google/gemini-3.6-flash` via
  `PAID_FLASH_FALLBACK`, within its caps. Every other paid model is refused by a guard in the code.
  OpenRouter key limit **$8** (Tony raised it from $1) is the hard ceiling; never top-up. If the key's
  usage is ever above the spend ledger, or above $1.00 in a UTC day: stop everything, tell the architect.
- Free-model use under 600 requests a day (OpenRouter `:free` limit is 1,000/day: it depends on credits
  ever purchased, so spending the $10 does not lower it).
- The bot runs only on GitHub Actions. Logs are public: never research, reasoning or forecast values.
- **Never a pure guess:** no real model forecast = nothing submitted, retried next run until the close.

## 3. Current live setup (2 Oct)

Everything has an on/off switch; the full list with PR numbers is NOTES.md "What's live".
- **Runs:** cron-job.org starts the tournament workflow every 10 min (`BOT_ENABLED=true`). Seasonal and
  MiniBench questions go in **one queue, researched soonest-closing first**.
- **Lineup `gemini-free`:** free Google AI Studio key. Forecasters Gemini 3.6/3.7/3.8/3.5 Flash, high
  reasoning; research planner, dossier and parser on Flash-Lite. Quota ledger counts every attempt
  (Google counts failed ones); a model failing twice in a run is skipped for that run.
- **Paid-Flash fallback (ON since 1 Oct 00:29 UTC):** each Flash slot tries the free models first; only
  if the whole free chain fails (503, 429, timeout, no quota) it makes ONE paid call to OpenRouter
  Gemini 3.6 Flash (high reasoning). Paid only fills a question up to **3 real forecasts** (extra slots
  free only). Caps: **$0.10 per question**, **$1.00 per UTC day**, **MiniBench only while the day's paid
  spend is under $0.50**. Reserves a measured $0.03 per call; unknown cost is charged $0.03; fail closed
  (ledger unreadable or missing, key usage unreadable or above the ledger -> no paid calls). Alert issue
  when the key's remaining limit drops under $2.
- **Backup chain:** no Flash answer and (closing within 45 min, or every Flash out of quota) -> Nemotron
  Ultra `:free` x2 first, Flash-Lite only if Nemotron gives nothing.
- **Extra forecasts** on spare free quota: up to 5 (binary) / 6 (numeric, MC); paid never fills them.
- **Research:** planner -> AskNews (max 3 calls/question; AskNews works again since 30 Sep) or free news
  (Google News RSS + GDELT) -> dossier with a CURRENT VALUE line -> gap-fill; **Wikipedia** background
  (<= 800 tokens); **official data line** (FRED/CoinGecko; "stand-in" warning when it isn't exactly what
  is asked). **Follow-up search** when round 1 disagrees. No market prices.
- **Combining:** binary median, stretch off (k=1.0), extreme check, clip 2-98%; MC median + 1% floor;
  numeric pointwise-median CDF + 5% uniform; unit check vs current value.
- **Timing caps (per run):** research 4 min per question; planned forecasts waited for until minute 9
  of a 12-minute forecasting cap (quick forecast gets minutes 9-12); no new question after minute 30;
  shadows only before minute 45 (end by 47). Worst case ~46-47 min, job limit 60.
- **Logs:** each question's JSON log saved right after submission; Nemotron shadow in
  `<log>_shadows.json` (the Scoreboard merges it).
- **Shadows (never submitted, scored):** stretch-1.2/1.5, mean, geo-mean, trimmed-mean; MC mean; numeric
  mean-cdf/uniform; **Nemotron shadow forecaster**; **consistency shadow** for sibling questions;
  **empirical window baseline** for official-data numeric questions (replaced the random walk, 1 Oct).
- **Replay lab:** nightly (last 3 h of the Pacific day, started by the bot itself); reruns variants on
  frozen dossiers after questions resolve (E1 3 vs 5 forecasts, E2 no follow-up, E3 no Wikipedia,
  E4 + Nemotron). First automatic run 1 Oct 04:09 UTC, green, 0 resolved questions yet.

## 4. Real numbers (to 2 Oct)

- **Free Flash in live runs since go-live (27 Sep 18:00):** 134 calls, **12 answered (9%)**, 119 x 503
  (89%), 3 x 429. Last 3 days better: 9 of 49 (18%). 503s come back in 1-8 s (refused at the door).
- **Flash probe (30 Sep, same question, 8 calls):** early UK morning 2/4 answered, UK daytime 0/4; high vs
  default reasoning made no difference -> 503s look like Google free-tier overload by time of day.
- **Paid Flash (1 Oct, first day):** 7 calls, 7 answered, $0.0227-0.0289 each (median 6,426 output
  tokens; OpenRouter's high reasoning writes ~3x more than Google's own API did in the probe), **$0.186 in
  all, ~$0.093 per question** before the 3-forecast rule (now about 3 x $0.027 = ~$0.08 at most).
  Key usage = our ledger, to the cent.
- **Budget:** $8 limit, $0.19 used, **$7.81 left** (~40+ days at the first day's pace). Daily report:
  paid calls, $, $/question, days left, forecasts per question.
- **Questions:** 1 Oct both questions got 6 forecasts (free + paid); before paid Flash most got 1.
- **Window baseline on 45868 (VIX high):** P(>40) 5.5% vs our live forecast 19.3% (models' heavy tail).

## 5. Leaderboard and the decisions it drove

FutureEval scores vs GPT-4o (from Tony's screenshots, in the project files): Metaculus community 25.2,
**Claude Fable 5 16.1**, Opus 5 15.8, Opus 4.8 14.1, **Gemini 3.6 Flash 13.2**, Kimi K3 12.9, GPT-5.6 Sol
12.9, Gemini 3.5 Flash 12.2, Gemini 3.1 Pro High 11.9, Kimi K2.6 9.6, GLM 5.2 9.2, Kimi K2.5 9.2,
DeepSeek V4 Pro 8.7, MiniMax M3 8.7, Grok 4.20 8.5, GLM 5.1 8.3, GLM-5 7.8, Qwen 3.6 Plus 7.3, Grok 4.6
7.2, Grok 4.3 7.0, Qwen 3.7 Plus 6.7, DeepSeek 3.2 6.5, **Gemini 3 Flash 6.4**, **Nemotron Ultra 5.8**,
**Gemma 4 2.5**, **Flash-Lite 1.9 / 0.7**. 3.7/3.8 Flash unscored (assumed >= 3.6, a guess).
- **Nemotron first in the backup chain** (5.8 vs Flash-Lite 1.9/0.7), Flash-Lite only if it fails.
- **Fable 5.1 removed** from the credits lineup: Fable 5 vs Opus 5 is within noise at 2.5x the price.
- **Gemini 3 Flash and Gemma dropped** (6.4 and 2.5; the 29 Sep probe: Gemini 3 Flash answered 1 of 4).
- **Paid Flash = Gemini 3.6 Flash** (13.2, cheap); high reasoning kept (R2: higher effort won 8/8).
- **Free-model scan (30 Sep):** none of the 15 leaderboard models above Nemotron is `:free` on OpenRouter.

## 6. Credits readiness (Metaculus credit key: not received)

- Built and OFF: `credits` lineup (Opus 5.5, GPT-5.6 Sol, Gemini 3.6 Flash; Full/Standard/Lean tiers by
  remaining credit; MiniBench one tier lower), backups, free-first Flash slot, pre-flight, real-price cost
  table (full ~$0.64/question at an ASSUMED 8k output tokens: measured paid Flash is ~6.4k), fail-closed
  spend guards (per-question 2x tier cost, daily 2x target, key-vs-ledger backstop, never re-buy).
- **Switch-on checklist (PLAN.md 6b):** 0. spend ledger exists (done 30 Sep, `{}` then paid-Flash data);
  1. Tony puts the credit key in `OPENROUTER_API_KEY`, his own key moves to a test-only secret (the paid-
  Flash fallback would then bill the credit key: decide); 2. Credit check shows the donated limit;
  3. Credits pre-flight green; 4. credits Test Bot on 4 types, real cost per question from the ledger;
  5. architect sets the starting tier one below what the numbers allow; 6. first-24-hour spend report;
  7. after 10 live questions, replace the assumed 8k output tokens with the measured 75th percentile.

## 7. Measurement and tuning candidates

Tools: weekly **Scoreboard** (Mondays 06:30 UTC; live + every shadow, log scores per type), **Replay
lab** (paired differences with 90% CI), Nemotron answer rate. Nothing switches on without data.

| Candidate | Decided by | When |
|---|---|---|
| Nemotron as a regular forecaster / backup order | shadow answer rate + scores, lab E4 | ~mid-Oct (2 weeks of shadow data) |
| 3 vs 5 forecasts, follow-up search, Wikipedia | Replay lab E1-E3 | first results ~mid-Oct |
| Stretch k, clip limits | Step 11 cross-validation | >= 150 resolved binary (~mid-Nov) |
| Blend with the window baseline (official-data numeric) | Scoreboard / lab | ~150 resolved questions |
| Referee shadow (needs credits) | >= 80 resolved binary, must beat the median | after credits |
| Market blend (off, code kept) | 0 of 20 candidates matched | not planned |
| 9-minute planned-forecast wait | raise if any live Flash forecast > 6 min (watched; longest 132 s) | ongoing |

## 8. Safety nets and operations

- **Daily health check** (started by the first tournament run after 06:00 UTC): red for a missed or
  about-to-be-missed question, no successful run for 3 h, **any workflow not "active" (named)**; warnings
  for low Gemini quota, AskNews 402, log-save failures, MiniBench quiet 3 days, **main's last commit
  older than 21 days**.
- **Keepalive daily** (05:00 UTC): calls GitHub's "enable workflow" API for every workflow (no commit).
- **21-day rule:** a small notes PR merges into main at least every 21 days (CLAUDE.md, standing).
- **Workflow check:** every PR runs "Validate workflows" (YAML parse + actionlint with shellcheck); a
  ruleset makes it required on main. Never merge with `--admin`.
- **Install cache:** Poetry and the `.venv` are cached (key: Python version + lock file); on a hit no
  package is downloaded; on a miss 3 tries, 20 s apart.
- **Emergency stop:** repository variable `BOT_ENABLED=false`.
- Test tools: Test Bot lineups `free`, `replay` (0 calls), `replay-credits`, `replay-gemini`,
  `real-regression`, rehearsals `burst-`, `timing-`, `paid-flash-rehearsal`; Model probe; Credit check.

## 9. Lessons (this session and before)

- **Wrong-shape baseline:** the random walk modelled the END value, but 45868 asked for a MAXIMUM; its
  10th percentile sat below today's VIX. Match the statistic to the question (now end/max/min).
- **A data gap stopped the baseline:** the 9/11 market closure (a 7-day gap) silently ended every later
  window; caught by checking the window count against the history length.
- **Pacific-day lab skip:** two manual proof runs counted as that Pacific day's lab run, so the first
  nightly run never started. Now only bot-started runs count.
- **Queue cancellations:** GitHub keeps one waiting run per concurrency group; a second one cancels it.
  Dispatch one test at a time, only when nothing is waiting.
- **iCloud:** it made " 2" copies of files, even in `.git`. The repo now lives in `~/code/fall26-bot`.
- **YAML:** `run: echo "a: b"` (a ": " in an unquoted value) breaks the file and the workflow won't start.
  Now caught by the required workflow check.
- **Leaderboard screenshots were in the project files all along:** look there before asking Tony.
- **Check data before claiming:** "no live Flash answer yet" was wrong (3.8 answered 3/3 on 27 Sep).
- Free models sometimes truncate a reply: a red free-model Test Bot gets one rerun after reading the log.
- On a personal repo a ruleset can't exempt GitHub Actions: the Keepalive had to stop pushing commits.
- Verify "free / no card" claims on the provider's own docs (Mistral mistake).

## 10. Open items and dates

| Date | Item |
|---|---|
| Fri 2 Oct | Email to Ben (Metaculus) about credits sent; wait for the reply |
| ~5 Oct | MiniBench burst expected (round 33125 runs to 9 Oct; quiet since 24 Sep); watch paid spend under the $0.50 MiniBench limit |
| ~mid-Oct | First Replay lab results; Nemotron 2-week shadow review |
| 19 Oct | `ubuntu-latest` moves to Ubuntu 26; our workflows are pinned to `ubuntu-24.04` (check the pin still works) |
| when they come | First resolutions -> Scoreboard and lab start scoring (45868 VIX: compare live vs window baseline) |
| ~mid-Nov | ~150 resolved binary -> Step 11 tuning |
| 6 Jan 2027 | Post-season survey (required for prizes); next-season participation form early Jan |
| 31 Jan 2027 | Tokens expire (`DATA_REPO_TOKEN` and other fine-grained tokens): renew before |

## 11. Where things are

- Code (public): `tonyelfilali-collab/fall26-bot`. Data (private): `tonyelfilali-collab/fall26-data`
  (`questions/`, `quota/gemini_free.json`, `status/spend.json`, `status/`, `reports/`, `lab/`, `probes/`).
- Secrets: `METACULUS_TOKEN`, `OPENROUTER_API_KEY` (Tony's key, limit $8), `ASKNEWS_API_KEY`,
  `GEMINI_API_KEY` (free AI Studio), `FRED_API_KEY`, `DATA_REPO_TOKEN`, `HEALTHCHECK_URL`,
  `METACULUS_READ_TOKEN` (Tony's read-only token, test bench only; the live bot refuses it).
- Daily paid-Flash report: Claude Code runs it after 00:10 UTC (spend ledger + question logs + a fresh
  Credit check).

## 12. Opening message for the next architect chat

> You are the architect for our Metaculus Fall 2026 forecasting bot. Read HANDOVER.md (v3) in the
> project files first, then CLAUDE.md and PLAN.md (progress log at the bottom). Tony relays messages
> between you and Claude Code, who builds and merges by the tier rules. Current state: the bot is live
> on the free Gemini key with a paid Gemini 3.6 Flash fallback ($0.10/question, $1.00/day, MiniBench
> under $0.50; $7.81 of $8 left); free Flash answers only ~9-18% of calls (503s). We are waiting for the
> Metaculus credit key (email to Ben sent 2 Oct), the MiniBench burst (~5 Oct) and the first resolutions
> (Replay lab results ~mid-October). Since then: [Tony adds the latest daily report and any news].
> First, confirm you have read the handover and list the next decisions you see.
