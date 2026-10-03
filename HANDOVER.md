# HANDOVER v3: Metaculus Fall 2026 bot (architect sessions 1–3, 27 Sep – 2 Oct 2026)

For the next architect chat. Replaces v2 (28 Sep). Written by the session-3 architect on Fri 2 Oct 2026, using Claude Code's draft (PR #124) checked against the repo. Added to the repo on Sat 3 Oct 2026.

**Read in this order**
1. This file.
2. Live repo files, read raw (they're newer than any project-file copy):
   - `https://raw.githubusercontent.com/tonyelfilali-collab/fall26-bot/main/CLAUDE.md` (rules, merge tiers)
   - `.../main/NOTES.md` ("What's live" table at the top: every switch, value and PR)
   - `.../main/PLAN.md` (progress log at the bottom; tuning candidates in Step 11; switch-on checklist)
   - `.../main/CC_HANDOVER.md` (Claude Code's side)
3. Project files `ARCHITECT_LOG.md` (session-3 decisions) and `claude_ARCHITECT_LOG.md` (session-2). Since 3 Oct this handover and the architect log are kept private: Claude project files, and fall26-data `notes/` (not in the public repo).
4. Background only: ARCHITECT_BRIEF.md, R1–R3, and the six leaderboard screenshots (section 5 lists the scores).

Repo (public): `tonyelfilali-collab/fall26-bot`. Data (private): `tonyelfilali-collab/fall26-data`.

---

## 0. Tony and how to work with him

- UK-based, not a developer, has a Claude Max plan, keen to build. He finds idle waiting frustrating: when waiting is right, say why in one line and offer the most useful safe work.
- **Short, plain, click-by-click.** No jargon. **One paste-ready box per Claude Code message.** When he says he's confused, re-explain in a few lines, not more detail.
- He gets frustrated when things go wrong that should have been caught. Own mistakes plainly, give the fix, keep it in proportion (one bad forecast among hundreds isn't a disaster), move on.
- He wants honesty about chances and pushback on bad ideas, with evidence (cite R1–R3 or real data) and a clear "this is a guess" when it is.
- He doesn't merge. Claude Code merges by tier (section 1).
- He opens Claude Code from the Claude desktop app, in the folder `~/code/fall26-bot` (moved out of iCloud on 30 Sep).

**How the architect works (tools)**
- Read raw code: `curl https://raw.githubusercontent.com/tonyelfilali-collab/fall26-bot/<branch>/<file>`. For a PR, find its branch on the PR page (`github.com/.../pull/N`, grep `tree/<branch>`), then read the files raw. `pull/N.diff` redirects to patch-diff.githubusercontent.com, which is blocked.
- Run status: `curl -sL github.com/.../actions/runs/ID` and grep `completed successfully` / `failed`. Workflow run lists: `github.com/.../actions/workflows/<file>`.
- `api.github.com` is usually rate-limited from the architect's shared IP. Don't rely on it.
- web_fetch only works on URLs that appear in the chat. metaculus.com is blocked (Cloudflare).
- Never trust summaries of diffs; reading the raw code found real bugs in session 3 (the spend guard's fail-open holes).

---

## 1. Roles and rules

- **Tony:** keys, a few clicks, relays messages.
- **Architect:** plans, reviews, decides, verifies.
- **Claude Code (Opus 5.5, Tony's Mac):** writes all code, merges by tier, runs background watchers, writes daily reports.

**Merge tiers (in CLAUDE.md)**
- **A** (notes, health, CI, tools the live run doesn't use): unit tests → merge.
- **B** (forecast plumbing, no prompt/model change): + replay Test Bot on 4 types → merge.
- **C** (prompts, models, answer reading, dossier): + real free-model Test Bot on 4 types → architect says merge (or pre-approves with explicit gates).
- **Always:** state the tier; the required "Validate workflows" check must pass (never `--admin`); check the next live run after each merge and revert if red; never two forecast-path merges between two live runs; stop an item after 2 failures; a progress-log row per merge; dispatch one test at a time (a second one cancels the waiting run).

**Hard rules**
- **Zero spend, one exception (Tony, 30 Sep):** the only paid model allowed is `google/gemini-3.6-flash` via `PAID_FLASH_FALLBACK`, within its caps (section 2). A code guard refuses every other paid model. The OpenRouter key limit is **$8** (of $10 bought); never top up without Tony. If the key's usage is ever above our spend ledger, or above $1.00 in a UTC day: stop and tell the architect.
- No human in the loop on forecasts. GitHub Actions only. One change per PR. Measure before shipping anything risky.
- **Never a pure guess.** No real model forecast → nothing submitted, retried until close. A skip scores 0; a coin-flip scores below 0 on average.
- Public logs never contain research, reasoning or forecast values.

---

## 2. Current live setup (2 Oct)

Full switch list with PRs: NOTES.md "What's live".

- **Triggering:** cron-job.org starts the tournament workflow every 10 min (`BOT_ENABLED=true`). GitHub's own schedule is unreliable in this repo. Seasonal and MiniBench questions share one queue, soonest-closing first.
- **Lineup `gemini-free`:** free Google AI Studio key. Forecasters Gemini 3.6/3.7/3.8/3.5 Flash (20 requests/day each), high reasoning. Planner, dossier and parser on Flash-Lite (500/day each; ledger 400). The quota ledger counts every attempt (Google counts failed ones); quota day = Pacific midnight (07:00 UTC now, 08:00 UTC from 1 Nov).
- **Paid-Flash fallback (ON since 1 Oct 00:29 UTC):** each Flash slot tries the free chain first; if it all fails, one paid call to OpenRouter Gemini 3.6 Flash (high reasoning). Paid only fills a question up to **3 real forecasts**. Caps: **$0.10/question, $1.00/UTC day, MiniBench only while the day's paid spend is under $0.50**. Reserves $0.03 per call. Fail closed (ledger or key usage unreadable → no paid calls). Alert issue when the key's remaining limit drops under $2.
- **Backup chain:** no Flash answer and (closing within 45 min, or every Flash out of quota) → Nemotron 3 Ultra `:free` ×2 (median), Flash-Lite only if Nemotron gives nothing.
- **Extra forecasts** on spare free quota: up to 5 (binary) / 6 (numeric, MC); never paid.
- **Research:** Flash-Lite planner → AskNews (max 3 calls/question; tournament allowance enabled 30 Sep; no card on the account) or free news (Google News RSS + GDELT) → dossier ≤6k tokens with a current-value line → **follow-up search** when round 1 disagrees → **Wikipedia** (≤800 tokens) → **official-data line** (FRED/CoinGecko, with a "stand-in" warning when it isn't exactly what's asked). No market prices. Yahoo and Stooq block us; no Google Search grounding on this key.
- **Combining:** binary median → stretch off (k=1.0) → market blend off → extreme check → clip 2–98%. MC median + 1% floor. Numeric: PCHIP per model → pointwise median → 95% + 5% uniform → platform checks. Unit check against the current value (drops flagged models; keeps all if all flagged).
- **Timing caps:** research 4 min/question; planned forecasts waited for until minute 9 of a 12-min cap (quick forecast gets 9–12); no new question after minute 30; shadows only before minute 45. Worst case ~46 min (job limit 60).
- **Logs:** each question's JSON saved right after submission; shadows in `<log>_shadows.json`.
- **Shadows (never submitted, scored weekly):** binary stretch-1.2/1.5, mean, geo-mean, trimmed mean; MC mean; numeric mean-CDF, 2% uniform; Nemotron shadow forecaster; consistency shadow for sibling questions (#78); **empirical window baseline** for official-data numeric questions (end/max/min over past windows of the same length; replaced the random walk, #110).
- **Replay lab (#92):** started nightly by the bot in the last 3 h of the Pacific day. Reruns variants on frozen dossiers once questions resolve: E1 3 vs 5 forecasts, E2 no follow-up, E3 no Wikipedia, E4 + Nemotron. First run 1 Oct 04:09 UTC, green, nothing resolved yet.
- **Credits lineup:** built and OFF (section 6).

---

## 3. Real numbers (to 2 Oct)

- **Questions so far:** 8 seasonal (45516, 45847, 45844, 45848, 45845, 45868, 45846, 45849). All forecast; none missed. MiniBench round 33125's 60 questions all opened 21–23 Sep and closed by 24 Sep, before go-live (not a miss). No MiniBench question since.
- **Free Flash, live, since 27 Sep 18:00:** 134 calls, **12 answered (9%)**, 119 × 503, 3 × 429. Last 3 days 9 of 49 (18%). 503s come back in 1–8 s. Probe (30 Sep): early UK morning 2/4, UK afternoon 0/4; reasoning setting made no difference → Google free-tier overload by time of day.
- **Before paid Flash,** most questions got **1 forecast** (or a weak backup). On 1 Oct, with paid Flash, both questions got **6**.
- **Paid Flash, 1 Oct:** 7 calls, 7 answered, $0.023–0.029 each (median 6,426 output tokens: OpenRouter's high reasoning writes ~3× what Google's API did in the probe). $0.186 total. Key usage matched our ledger exactly. **$7.81 of $8 left.** With the 3-forecast rule, expect ~$0.05–0.08/question.
- **Daily report** (Claude Code, after 00:10 UTC): paid calls, $, $/question, days of budget left at the 7-day pace, forecasts per question, free vs paid vs backup.
- **45868 (VIX intraday high, 1 Oct–24 Dec):** live P(>40) 19.3% (median 28.7); window baseline 5.5% (median 21.4). Both saved for scoring. Live tail looks too heavy, but no change on one question.

---

## 4. Decisions (session 3, 28 Sep evening – 2 Oct)

| Decision | Why |
|---|---|
| Flash-Lite ledger 400/day (was 20) | AI Studio page: Flash-Lite 500/day; Flash 20/day; over-limit attempts count |
| Nemotron first in the backup chain, ×2; Flash-Lite only if Nemotron fails | Leaderboard: Nemotron 5.8 vs Flash-Lite 1.9/0.7 |
| Gemini 3 Flash, Gemma dropped; Google Search grounding impossible | Scores 6.4 / 2.5; Gemini 2.5 closed to new users; Gemini 3 grounding 0/day |
| No free model better than Nemotron | Scan of 15 leaderboard models: none is `:free` on OpenRouter |
| Credits readiness built OFF: pre-flight, real-price cost table, free first, fail-closed spend guards, switch-on checklist | So credits can't be wasted on day one |
| Fable 5.1 removed; credits round 2 = one more Opus 5.5, GPT-5.6 Sol, Gemini 3.6 Flash | Fable 5 vs Opus 5 within noise at 2.5× price; balanced families (R3) |
| Separate concurrency queue for non-Gemini tests | A test dispatch could cancel a waiting live run |
| Extra forecasts on spare quota; follow-up search on disagreement; Wikipedia; all with switches | Aggregation and research breadth are the strongest evidence (R2, R3) |
| Replay lab on frozen dossiers | Only leak-free way to measure changes (R3); CP not available |
| Timing caps, per-question log saves | Burst of 5 could exceed 60 min and lose logs |
| **Paid-Flash fallback (Tony's yes, 30 Sep)**; 3-forecast rule; MiniBench ≤$0.50/day; $0.10/question | Free Flash answers ~9%; 1 → 3 forecasts is the big gain; seasonal pool worth far more per question |
| High reasoning kept on paid Flash | R2: higher effort won 8/8 |
| Window baseline replaces random walk | Random walk modelled the END value; 45868 asks a MAXIMUM |
| Install cache + retries; required workflow check; daily keepalive; health red on a non-active workflow; 21-day notes-PR rule | PyPI outage, a YAML near-miss, 60-day inactivity pause |
| Pause feature building except evidence-driven fixes | R2: dev hours correlated negatively with rank; bottlenecks are credits and resolved questions |

---

## 5. Leaderboard (FutureEval, score vs GPT-4o, 27 Sep; screenshots in project files)

Pros 34.2, Community 25.2, **Fable 5 16.1**, Opus 5 15.8, Opus 4.8 14.1, **Gemini 3.6 Flash 13.2**, Kimi K3 12.9, GPT-5.6 Sol High 12.9, Gemini 3.5 Flash 12.2, Gemini 3.1 Pro High 11.9, GPT-5.5 High 11.4, … Gemini 3 Flash 6.4, **Nemotron 3 Ultra 5.8**, Gemma 4 2.5, Gemini 3.1 Flash-Lite 1.9, Gemini 3.5 Flash-Lite 0.7. Gemini 3.7/3.8 Flash, Opus 5.5 and Fable 5.1 unscored (assumed ≥ their predecessors: a guess based on the version trend). CIs overlap heavily among the top ~10. These rows are Metaculus's own `metac-*` reference bots, not competitors.

---

## 6. Credits (not received)

- **Asked:** form 27 Sep ($750). **Email to Ben sent Fri 2 Oct** (credits + whether Ramp pays to UK banks).
- **Credits lineup (OFF):** round 1 Opus 5.5, GPT-5.6 Sol, Gemini 3.6 Flash; round 2 one more of each; numeric/MC all 6. Backups Opus 5.5→Opus 5, GPT-5.6 Sol→GPT-5.5, Gemini 3.6 Flash→Gemini 3.1 Pro. Gemini slot uses the free AI Studio key first. Tiers Full/Standard/Lean from remaining credit; MiniBench one tier lower.
- **Cost (real prices, ASSUMED 8k output tokens):** full ~$0.64/question, standard ~$0.39, lean ~$0.26. Paid Flash showed ~6.4k output, but Claude/GPT high reasoning may write more: measure on day one.
- **Spend guards (#81):** per-question cap 2× tier cost, daily cap 2× target (then Lean + alert), key-vs-ledger backstop, unknown cost charged the estimate, never re-buy finished forecasts, fail closed.
- **Switch-on checklist** (PLAN.md): 0 ledger exists (done); 1 credit key into `OPENROUTER_API_KEY`, Tony's key to a test-only secret; 2 Credit check; 3 pre-flight; 4 credits Test Bot on 4 types, real cost per question; 5 architect sets the start tier one below what the numbers allow; 6 24-hour spend report; 7 after 10 questions replace the assumed 8k with the measured 75th percentile.
- **Decided:** when the credits lineup goes live, `PAID_FLASH_FALLBACK` no longer applies (it belongs to `gemini-free`). Re-check that the spend ledger and caps are set for the credits lineup before step 4.

---

## 7. Measurement and tuning candidates

Nothing switches on without data. Tools: weekly **Scoreboard** (Mondays; live + every shadow, log score per type), **Replay lab** (paired differences, 90% CI), daily report.

| Candidate | Decided by | When |
|---|---|---|
| Nemotron as a regular voice / backup order | shadow answer rate + score, lab E4 | ~mid-Oct |
| 3 vs 5 forecasts, follow-up search, Wikipedia | lab E1–E3 | first results ~mid-Oct |
| Blend with the window baseline (official-data numeric) | Scoreboard / lab | ~150 resolved |
| Stretch k, clip limits | Step 11 cross-validation | ≥150 resolved binary (~mid-Nov) |
| Referee (needs credits) | must beat the median on ≥80 resolved binary | after credits |
| 9-min planned-forecast wait | raise only if a live Flash forecast exceeds 6 min (longest so far 132 s) | watched |

---

## 8. Safety nets and operations

- **Health check:** started daily by the first tournament run after 06:00 UTC. Red: missed or about-to-be-missed question, no successful run for 3 h, any workflow not "active" (named). Warnings: low Gemini quota, AskNews failing, log-save failures, MiniBench quiet 3 days (currently firing: normal), main's last commit >21 days.
- **Keepalive:** daily 05:00 UTC, "enable workflow" API for every workflow. **21-day rule:** a notes PR merges at least every 21 days.
- **healthchecks.io** pinged every run. **Emergency stop:** repository variable `BOT_ENABLED=false`.
- **Install cache** (Poetry + `.venv`, key: Python version + lock file); on a miss 3 tries, 20 s apart. Python must be **3.11** (3.14 fails 51 tests).
- **Claude Code watchers** (restart in every new Claude Code session, CC_HANDOVER section 3): red live run/health; 5 distinct questions (done); slow Flash (>6 min); Replay lab; daily paid-Flash report.
- An old cron-job.org health job fires around 7 PM: harmless, not needed.

---

## 9. Ideas rejected (don't reopen without new evidence)

All from v2 still stand (personas, forced reasoning templates, best-of-k, stacking, fine-tuning, date-filtered backtests, dashboards, √3 extremizing everywhere, late re-forecast, showing market prices to forecasters, Tony's Claude Max as a forecaster, extra Google keys for quota, using the community prediction). Added in session 3:
- Gemini 3 Flash or Gemma as forecasters (6.4 / 2.5; 503-prone).
- Search grounding (impossible on this key); Yahoo and Stooq (block us).
- Fable 5.1 in the credits lineup (price vs Opus).
- Paying for forecasts beyond 3 per question on the paid fallback.
- More features without data (complexity costs points, R2).

---

## 10. Lessons

- **Check what's in the project files before asking Tony** (the leaderboard screenshots were there all along; the session-3 architect asked for them twice).
- **Check the question's details before giving a number** (VIX window was ~3 months; the first estimate was too low).
- **Match a baseline to the question's statistic** (end vs max vs min). Check window counts against history length (a 9/11 data gap silently cut everything after 2001).
- **Read raw code for anything with money or safety.** The first spend-guard draft charged unknown costs as $0, waved through calls with no question, and started from $0 when the ledger failed to load.
- Leaderboard evidence trumps intuition: "Nemotron + Flash-Lite together" was wrong once scores were checked.
- Manual proof runs can block scheduled ones (Pacific-day lab skip). Test runs can cancel waiting live runs (shared queue).
- iCloud Desktop corrupts repos (" 2" copies inside `.git`). YAML `": "` in an unquoted value kills a workflow. A personal-repo ruleset can't exempt GitHub Actions.
- Verify "free, no card" and quota claims on the provider's own pages. Claude Code corrects itself when shown data; trust run links, not summaries.

---

## 11. Open items, dates and honest outlook

| When | Item |
|---|---|
| Any day | Ben's reply (credits, Ramp UK). If credits: follow the switch-on checklist. |
| ~5–9 Oct | Next MiniBench round (~60 questions in ~3 days). Watch forecasts per question and paid spend under the $0.50 MiniBench limit. New rounds are found within one run (#90). |
| Daily | Paid-Flash report: budget left, 3+ forecasts per question. Alert at <$2 left → Tony decides (top up vs fall back to free). |
| ~mid-Oct | First lab results; Nemotron 2-week review; if no credits, decide with numbers whether to extend the paid-Flash budget. |
| 19 Oct | GitHub moves `ubuntu-latest` to Ubuntu 26; we're pinned to 24.04 (check). |
| ~mid-Nov | ~150 resolved binary → Step 11 tuning. |
| 6 Jan 2027 | Post-season survey (required for prizes); next-season form early Jan. |
| 31 Jan 2027 | Fine-grained tokens expire (data token, cron token): renew before. |

**Honest outlook (tell Tony straight)**
- **Now (free Gemini + paid Flash fallback):** a solid mid-table bot. Reliability is strong (zero misses, zero disasters, every merge checked). Forecast quality is limited to one model family; MiniBench (small, noisy fields) is the likeliest prize chance.
- **With credits:** a real shot at the top 10–25, where the prizes are. The top 10 last season weren't statistically separable (R2), so luck matters too.
- The edge we control: never missing, no disasters, a cross-family median, and changes measured on resolved questions.

---

## 12. Opening message for the next architect chat (Tony pastes this)

> You are the lead architect for my Metaculus Fall 2026 FutureEval forecasting bot. Read HANDOVER.md (v3) in the project files fully first. Then read the live repo files raw (they're newer than any project copy): CLAUDE.md, NOTES.md ("What's live"), PLAN.md (progress log at the bottom) and CC_HANDOVER.md, from https://raw.githubusercontent.com/tonyelfilali-collab/fall26-bot/main/. The project files ARCHITECT_LOG.md (session 3) and claude_ARCHITECT_LOG.md (session 2) have the decisions. ARCHITECT_BRIEF, R1–R3 and the leaderboard screenshots are background.
>
> How we work: I'm a UK novice. Give me short, plain, click-by-click steps and one paste-ready box for each Claude Code message. If I say I'm confused, re-explain simply and briefly. Claude Code merges by the tier rules; Tier C needs you to say merge (or pre-approve with gates). Check PRs and run pages yourself and read raw code for anything important. Hard rules: zero spend except the paid Gemini 3.6 Flash fallback within its caps ($8 key limit), no human in the loop, GitHub Actions only, one change per PR, measure before shipping anything risky. Be honest about our chances and push back on bad ideas, with evidence; say when you're guessing.
>
> Latest since the handover: [paste the latest daily paid-Flash report and any news, e.g. Ben's reply or MiniBench].
>
> Your first reply: (1) a 5-line status; (2) anything risky you see; (3) the next decisions due, with dates; (4) one paste-ready message for Claude Code if anything needs doing.
