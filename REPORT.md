# REPORT: autonomous build, 27–28 Sep 2026

Items 0–6 of the architect's long build, plus PR #17. Every code PR was merged only after its
unit tests and a free-model Test Bot run were green on all 4 question types. After each merge,
the next cron-started tournament run was checked: all green. No revert was needed.
Live tournament runs since 21:00 UTC on 27 Sep: **27 of 27 green** (no new questions yet).

## What was done

| Item | PR | Unit tests | Test Bot (free, 4 types) | Next cron run after merge |
|---|---|---|---|---|
| Quota check → warning | [#17](https://github.com/tonyelfilali-collab/fall26-bot/pull/17) | [36347674330](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36347674330) | – (health check only) | green (later runs) |
| 0. PLAN wording (never a pure guess) | [#20](https://github.com/tonyelfilali-collab/fall26-bot/pull/20) | notes only | – | – |
| 1. Step 8 numeric + multiple choice | [#21](https://github.com/tonyelfilali-collab/fall26-bot/pull/21) | [36348117860](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36348117860) (116) | [36348117052](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36348117052) | [36348397677](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36348397677) |
| 2. Step 7 research | [#22](https://github.com/tonyelfilali-collab/fall26-bot/pull/22) | [36348493903](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36348493903) (130) | [36348493228](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36348493228) | [36349615561](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36349615561) |
| 3. Step 6 smart ensemble (credits-ready) | [#23](https://github.com/tonyelfilali-collab/fall26-bot/pull/23) | [36349581195](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36349581195) (153) | [36360696544](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36360696544) (1st try [36349277676](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36349277676) hit the OpenRouter free limit) | [36361318948](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36361318948) |
| 4a. Read answers without a parser call (extra) | [#24](https://github.com/tonyelfilali-collab/fall26-bot/pull/24) | [36360982209](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36360982209) (171) | [36360986095](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36360986095) | [36361969188](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36361969188) |
| 4. Step 5 test bench | [#25](https://github.com/tonyelfilali-collab/fall26-bot/pull/25) | [36361435697](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36361435697) (181) | [36361440046](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36361440046) | [36362572301](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36362572301) |
| 4. bench fix | [#28](https://github.com/tonyelfilali-collab/fall26-bot/pull/28) | [36362707475](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36362707475) | [36362706557](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36362706557) | [36363794506](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36363794506) |
| 5. Step 9 markets, log-only | [#26](https://github.com/tonyelfilali-collab/fall26-bot/pull/26) | [36362089496](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36362089496) (191) | [36362097621](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36362097621) | [36363177119](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36363177119) |
| 6. Step 10 shadow system | [#27](https://github.com/tonyelfilali-collab/fall26-bot/pull/27) | [36363302526](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36363302526) (203) | [36363309230](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36363309230) | [36364280480](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36364280480) |

Progress-log rows are in PLAN.md; details for each item are in NOTES.md.

## What's live now (lineup `gemini-free`, BOT_ENABLED = true, cron-job.org every 10 min)

- **Research (Step 7):** a Flash-Lite planner writes up to 3 queries; AskNews max 3 calls per question
  (free news when AskNews fails or finds <3); a Flash-Lite dossier ≤6k tokens with no market prices;
  one gap-fill search.
- **Forecasts (Step 6):** up to 3 different Gemini Flash models (3.6/3.7/3.8/3.5) per question; for
  binary, a round 2 of up to 2 more models only when round 1 disagrees (>15 pts) or is extreme.
  Answers are read directly ("Probability: ZZ%", option lines, percentile lines); the Flash-Lite
  parser is used only when that fails.
- **Combining (Steps 4 + 8):** binary median → stretch k=1.0 (off) → extreme check → clip 2–98%;
  multiple choice median per option + 1% floor; numeric/discrete PCHIP per model, pointwise
  median, 95% + 5% uniform, platform CDF checks. Never a pure guess.
- **Markets (Step 9):** log-only. After a binary forecast is submitted, candidates are found,
  judged by the Flash-Lite parser (1 call) and saved. Never blended.
- **Shadows (Step 10):** `stretch-1.2` and `mean` are saved for binary questions and never submitted.
- Never-miss: per-question JSON logs, healthchecks.io ping, daily health check, deadline cut-off, quick
  forecast, retries on later runs.

## What's switched off

- The `credits` lineup (PLAN section 3 models, backups, Full/Standard/Lean tiers, tier-change issue):
  built and tested, but `ACTIVE_LINEUP = "gemini-free"`.
- The referee shadow (`shadow.REFEREE_ENABLED = False`).
- Market blending (`MARKET_MODE = "log-only"`); multiple-choice market mapping isn't built.
- Stretch (`STRETCH_K = 1.0`).
- Test bench full run (waits for credits). **The quick proof didn't pass**, see below.

## Did not pass / stopped

**Item 4, test bench quick proof: stopped after two failed attempts** (per the rules).
1. [36362046228](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36362046228): crashed. The library only allows the community-prediction filter for binary
   questions. Fixed in [#28](https://github.com/tonyelfilali-collab/fall26-bot/pull/28).
2. [36363257308](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36363257308): ran, but found **0 questions with a visible community prediction**, so it
   scored nothing. Most likely Metaculus doesn't show the community prediction to our **bot
   account** (the token the bench uses), so the bench has nothing to compare against. The empty
   question list it saved was deleted so it can't be reused by mistake.
The bench code, tests and workflow are merged and work as far as they can. It needs a reference
it's allowed to see (see questions below).

## Market matches: first 20 candidates (log-only)

From the manual **Market matches** run [36363926848](https://github.com/tonyelfilali-collab/fall26-bot/actions/runs/36363926848) on 5 random open main-site binary
questions (judge: the free OpenRouter model). **None was accepted**; the judge rightly rejected
every one as a different event. The keyword search mostly finds loosely related markets.

| # | Question | Source | Market | Price | Volume | Fresh | Same event | Same source | Same deadline | Accepted | Judge's reason |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 39134 | polymarket | [US Government removes public access to another major AI model in 2026?](https://polymarket.com/event/us-government-removes-public-access-to-another-major-ai-model-in-2026-20260703202936862) | 16% | 37,912 | yes | no | no | no | no | The market concerns US government removal of public access to a major AI model, which is u |
| 2 | 39134 | polymarket | [US Government removes public access to a major Chinese AI model in 202](https://polymarket.com/event/us-government-removes-public-access-to-a-major-chinese-ai-model-in-2026-20260703203328223) | 16% | 53,390 | yes | no | no | no | no | The market concerns US government removal of public access to a major Chinese AI model, un |
| 3 | 39134 | kalshi | [Healthcare AI adoption in September Above 43.75%](https://kalshi.com/markets/kxaihealth) | 64% | 735 | no | no | no | no | no | The market concerns healthcare AI adoption rate exceeding 43.75% in September 2026, which  |
| 4 | 39134 | kalshi | [Healthcare AI adoption in September Above 44%](https://kalshi.com/markets/kxaihealth) | 36% | 0 | no | no | no | no | no | The market concerns healthcare AI adoption rate exceeding 44% in September 2026, unrelated |
| 5 | 39324 | polymarket | [Will Eddie Howe be the next head coach of the England national team?](https://polymarket.com/event/next-england-national-team-head-coach-20260731023421458) | 27% | 4,766 | yes | no | no | no | no | The market predicts Eddie Howe becoming England's head coach, unrelated to a civilian bein |
| 6 | 39324 | polymarket | [Will Pep Guardiola be the next head coach of the England national team](https://polymarket.com/event/next-england-national-team-head-coach-20260731023421458) | 5% | 80 | yes | no | no | no | no | The market predicts Pep Guardiola becoming England's head coach, unrelated to the forecast |
| 7 | 39324 | kalshi | [Which cities will the National Guard deploy to this year? Chicago](https://kalshi.com/markets/kxnguardcity) | 22% | 5,093 | no | no | no | no | no | The market predicts National Guard deployment to Chicago before 2027-01-01, not a civilian |
| 8 | 39324 | kalshi | [Which cities will the National Guard deploy to this year? Houston](https://kalshi.com/markets/kxnguardcity) | 16% | 2,169 | no | no | no | no | no | The market predicts National Guard deployment to Houston before 2027-01-01, not a civilian |
| 9 | 38867 | polymarket | [OpenAI resumes training by September 30, 2026?](https://polymarket.com/event/openai-resumes-training-by) | 2% | 91 | yes | no | no | no | no | The market predicts whether OpenAI will resume training by a specific date in 2026, which  |
| 10 | 38867 | polymarket | [OpenAI resumes training by October 5, 2026?](https://polymarket.com/event/openai-resumes-training-by) | 20% | 111 | yes | no | no | no | no | This market concerns OpenAI resuming training by October 5, 2026, a different event and ti |
| 11 | 38867 | kalshi | [Will OpenAI pay a tort claim with more than $1 million in damages befo](https://kalshi.com/markets/kxoaidamage) | 55% | 17,978 | yes | no | no | no | no | The market asks if OpenAI will pay a tort claim over $1 million before 2028, which is unre |
| 12 | 38867 | kalshi | [OpenAI corporate adoption rate in September Above 39.9%](https://kalshi.com/markets/kxopenadopt) | 50% | 0 | no | no | no | no | no | This market predicts whether OpenAI’s corporate adoption rate in September 2026 exceeds 39 |
| 13 | 39169 | polymarket | [League City, Texas passes ballot measure approving the police use of F](https://polymarket.com/event/league-city-texas-passes-ballot-measure-approving-the-police-use-of-flock-cameras) | 66% | 403 | yes | no | no | no | no | The market concerns a ballot measure in League City, Texas about police use of Flock Camer |
| 14 | 39169 | polymarket | [Manchester City punishment announced by October 9?](https://polymarket.com/event/manchester-city-punishment-announced-by) | 7% | 1,257 | yes | no | no | no | no | The market concerns sanctions announced against Manchester City FC, which is unrelated to  |
| 15 | 39169 | kalshi | [NYC population change (July 2025 – July 2027)? Increase 0.01-0.99%](https://kalshi.com/markets/kxpopchangenyc) | 41% | 28,274 | no | no | no | no | no | The market concerns NYC population change (increase 0.01-0.99%) between July 2025 and July |
| 16 | 39169 | kalshi | [NYC population change (July 2025 – July 2027)? Decrease 0-0.99%](https://kalshi.com/markets/kxpopchangenyc) | 34% | 46,348 | yes | no | no | no | no | The market concerns NYC population change (decrease -0.99% to 0%) between July 2025 and Ju |
| 17 | 39173 | polymarket | [Will the Federal decree of 19 June 2026 on additional funding for the ](https://polymarket.com/event/switzerlands-november-referendum-what-will-pass-20260713145059520) | 66% | 1,368 | yes | no | no | no | no | The market concerns a Swiss referendum on a VAT increase for AVS funding, which is unrelat |
| 18 | 39173 | polymarket | [Will the Popular initiative ‘For a restriction on fireworks’ be approv](https://polymarket.com/event/switzerlands-november-referendum-what-will-pass-20260713145059520) | 21% | 6,955 | yes | no | no | no | no | The market concerns a Swiss popular initiative to restrict fireworks, unrelated to NIH/NSF |
| 19 | 39173 | kalshi | [Which agencies will Trump eliminate? EPA](https://kalshi.com/markets/kxagencyelim) | 15% | 18,205 | no | no | no | no | no | The market asks whether Trump will eliminate the EPA before Jan 20 2029, which is a differ |
| 20 | 39173 | kalshi | [Which agencies will Trump eliminate? USAID](https://kalshi.com/markets/kxagencyelim) | 11% | 308,212 | yes | no | no | no | no | The market asks whether Trump will eliminate USAID before Jan 20 2029, unrelated to NIH/NS |

## Questions for the architect

1. **Test bench reference.** Our bot account can't see community predictions, so the bench can't
   score against them. Options:
   (a) score against a market price where the judge accepts an exact match;
   (b) switch to resolved questions (log score), which costs time rather than visibility;
   (c) check with Metaculus whether a bot can read the community prediction on main-site questions.
2. **Market search quality.** 0 of 20 candidates matched. Should the Step 7 planner's queries (already
   made per question) be reused for the market search, or should matching be limited to question
   types where markets are common (elections, macro, crypto)? Manifold returned no binary candidates
   in this sample.
3. **Parser quota.** Live, each question can use the Flash-Lite parser models for: planner (1),
   dossier (1), market judge (1), plus fallback parses. With 3 Flash-Lite models × 20/day (if
   `gemini-3.1-flash-lite-preview` really has its own quota), that's about 15–20 questions a day.
   Should the market judge (log-only, 1 call) stay on while quota is this tight?
4. **OpenRouter free limit (~50 requests/day)** limits testing: about 5–6 Test Bot runs a day. Fine
   for now; the credit key will lift it.
5. **Spending tiers** assume about 12 questions a day and the season end 6 Jan 2027: retune once we
   see the real rate.
6. **Referee and stretch:** they wait for data. `Shadow scores` can report once binary questions resolve.

## Not built / left for later

- Step 7: model web search via OpenRouter (no free option), and the "round-1 models disagree" gap-fill.
- Step 4: the "10× the current value" unit check (needs a structured current value from research).
- Step 9: blending, and multiple-choice mapping.
- Step 6: a separate fast-path model set on credits (the quick forecast uses Opus 5.5 → Opus 5).

## Update 28 Sep 2026 (06:00–08:30 UTC)

Merged (each followed by a green live run): #37 regression pack (A), #36 replay Test Bot (B),
#38 MC confident-answer fix (B), #39 notes/merge tiers, #40 credits rehearsal `replay-credits` (B),
#42 health missed-check fix (A), #41 MiniBench rollover (B).

- Credits rehearsal (#40): green, 0 model calls; standard tier seasonal, lean MiniBench, binary
  round 2 ran. Forced failure of Opus 5.5: backup Opus 5 answered every slot, all submitted.
  Runs 36391448898, 36391621349.
- MiniBench (#41): was the fixed slug `minibench` (round 33125, closes 9 Oct). Now: slug if running,
  else the remembered round, else a paced scan of the next 60 tournament ids (MiniBench rounds are
  unlisted: the API tournament list doesn't show them), at most hourly. Health warns after 3 days
  with no open MiniBench question. Tournament info run 36393104727.
- Health check bug (#42): every question closed in the last 24 h was reported "missed", because
  the library ignores `is_previously_forecasted_by_user=False`. Seasonal 45516 was reported missed
  but was forecast and submitted (27 Sep 18:02 UTC).
- **Health check has never run on its schedule** (GitHub schedules don't fire in this repo, same as
  the tournament cron). Needs a cron-job.org job for health.yml (06:00 UTC), like the tournament one.
- Test bench: DROPPED (community prediction not in the API).
- Waiting: #34 (unit check, Tier C) real Test Bot at 00:00 UTC, then the architect's answer.

## Update 28 Sep 2026 (08:40–09:35 UTC)

Merged (next live run green after each): #44 shadow variants for every type (B), #45 urgent
MiniBench first (B), #46 round 2 limit below half quota (B).
- MC floor-0.5% shadow is computed from each model's raw answer (the library lifts options under
  ~0.99%, which likely mirrors the platform minimum, so 0.5% may not be submittable live).
- Flash-Lite separate quotas: unknown; only gemini-3.5-flash-lite has ever been called (max 9 a day),
  no 429 from any Flash-Lite model in any run.
- Second free test provider: Mistral free plan recommended (no card, $10/month credit); waiting for
  the architect.

## Update 28 Sep 2026 (11:00–14:45 UTC)

- OpenRouter: new key with credit (not free tier), key limit $1, usage $0 at every check.
- #34 unit check merged (architect): flagged models dropped, all kept if every model is flagged.
- Answer reading: how each answer was read is logged (#51); real-regression Test Bot option (#52);
  two reading fixes from real free-model replies (#53 `Option_A` letter labels, #54 look-alike
  dashes): the 37-question pack went from 31 to 37/37 read directly. Question-log save retry (#55).
  Test Bot `free_model` option (#57, test-only).
- **First real tournament question 45847 (discrete, 14:00–17:00 UTC):** from 14:02 every Gemini
  Flash forecasting model answered HTTP 503 (Google overloaded) on every retry; Flash-Lite still
  answered (research dossier written). Not quota, not our code. The bot retries every 10 minutes
  and never guesses. Architect asked whether Flash-Lite may be a last-resort forecaster.

## Update 28 Sep 2026 (14:45–18:00 UTC) and handover

- **First real question 45847** (discrete, closed 17:00): Gemini Flash answered 503 all afternoon,
  then 429 daily quota after ~20 failed attempts each (0 successes). Emergency Flash-Lite fallback
  (#59); 45847 was **submitted at 16:34** with 1 forecast from gemini-3.1-flash-lite (run 36451749479).
- **Quota diagnosis:** Google counts failed attempts against the 20/day. Fix #60: the ledger
  counts attempts, 2-failure skip per model per run, 4 attempts per model per question per day,
  per-question retry backoff. Flash-Lite's real daily limit is unknown (38 attempts, no 429).
- Merged: #61 Nemotron 3 Ultra shadow forecaster (after submission, never submitted), #62 official
  data from FRED/CoinGecko (log only; 4/21 pack questions matched, all correct), #63 random-walk
  baseline shadow. Next live run green after each. OpenRouter usage $0 (limit $1).
- **Handover:** `CC_HANDOVER.md` has the state, rules, watcher commands and pending items.

## Update 28–29 Sep 2026 (18:00–01:10 UTC), architect session 3

Every PR below was merged by its tier's gates; the next live run after each was green and the
Credit check stayed at $0 (limit $1). No revert needed.

| PR | What | Tier | Gate runs | Live run after |
|---|---|---|---|---|
| #69 | Per-model Gemini limits (Flash-Lite 400/day, 12/min; preview shares 3.1's bucket) | B | replay 36467022200 | 36468231832 |
| #70 | Model probe (test-only) | A | probe 36468432007 | – |
| #71 | No-Gemini Test Bots get their own queue; data-repo save retry | B | replay 36471717846 | 36472915458 |
| #72 | Credits 4a pre-flight | A | preflight 36472633695 | – |
| #73 | Credits 4b real cost table | B | replay-credits 36473804170 | 36475210302 |
| #67 | Nemotron backup chain (Nemotron ×2 first, Flash-Lite only if it gives nothing) | C (pre-approved) | replay 36470977732, forced exhaustion 36504745553, real 36501212269 | 36501914704 |
| #68 | Official data line in the dossier; exact match feeds the unit check | C (pre-approved) | replay 36502153817, real 36502300528 | 36502751311 |
| #74 | Credits 4c free first | B | replay-credits 36503009094 | 36503583722 |
| #81 | Credits 4d fail-closed spend guards (replaces #75) | B | replay-credits 36503861121 + runaway 36504059309, retry-run 36504247834, unknown-cost 36504371284, missing-ledger 36504501438, key-mismatch 36504626571 | 36505172775 |
| #78 | Consistency shadow for sibling questions | B | replay 36505395203 | 36505962251 |
| #79 | Credits: Fable 5.1 removed | C (OFF lineup) | replay-credits 36506179609 | 36506768736 |

- Probe (#70): Gemini 2.5 Flash / Flash-Lite answer 404 "no longer available to new users" → no
  Search grounding on this key. gemini-3-flash answered 1 of 4 (3 × 503), gemma-4-31b 503; both dropped.
- Cost per question (4b, measured prompts + ASSUMED 8k output, after Fable removal): full $0.64,
  standard $0.39, lean $0.26. Only 1–2 logged questions per type so far.
- Credits lineup is still OFF. Switch-on checklist: PLAN.md section 6b.

## Update 29 Sep 2026 (06:30–09:30 UTC)

| PR | What | Tier | Gate runs | Live run after |
|---|---|---|---|---|
| #83 | Tournament info `--audit` (read-only) | A | audit 36540542752 | – |
| #84 | Health check started by the first tournament run after 06:00 UTC | B | replay 36540980282 | 36542287279 (started health 36542374417) |
| #85 | More forecasts when Flash quota is spare (up to 5 / 6) | C (pre-approved) | replay 36543190072, replay-gemini 36543467516, real 36544531697 (1st try 36543728238: OpenRouter 503) | 36545459916 |
| #86 | Disagreement follow-up search | C (pre-approved) | replay 36545434504, replay-gemini 36545668198, real 36545924191 | 36547635164 |
| #87 | Wikipedia background; Yahoo Finance data | C (pre-approved) | replay 36547903370, real 36548079745, Credit check 36548826382 | 36548724506 |

- MiniBench audit: round 33125 (21 Sep–9 Oct) had 60 questions, each open ~3 hours, all opened
  21–23 Sep (last closed 24 Sep 02:49 UTC), before the bot went live (27 Sep 18:00). None could
  have been forecast; no new MiniBench question since. Seasonal: 2 questions so far, both forecast.
- Yahoo Finance answers HTTP 429 to both Actions and a home connection (even the cookie/crumb
  handshake yfinance uses): skipped gracefully (error saved, no line). Wikipedia works.

## Update 29 Sep 2026 (09:30–10:30 UTC)

| PR | What | Tier | Gate runs | Live run after |
|---|---|---|---|---|
| #89 | Spare-quota rule: 25/day while MiniBench is active | B | replay 36550995079 | 36551941613 |
| #90 | MiniBench: new round found within one run | B | replay 36551598294 | 36553024092 |
| #91 | MiniBench burst rehearsal | B | burst 36552294108, replay 36552394780 | 36554092243 |
| #92 | Replay lab | B (pre-approved) | replay 36553665680, synthetic lab 36555610672, real lab 36555713026 | 36555144038 |

- Burst rehearsal (seed 29; seeds 1–10 all pass): 60/60 MiniBench and 6/6 seasonal questions forecast
  before close (all by Flash, 2.3 and 2.8 forecasts each, max 8 min after opening); Flash at most
  16/20 per model a day; Flash-Lite reserve never used outside a question's last 45 min.
- Stooq: its CSV URL serves a JavaScript bot challenge and robots.txt disallows all but Google/Bing:
  not used (stopped as instructed).

## First real questions: review (30 Sep 2026)

Six real seasonal questions so far (45516 before question logs existed). Forecast values are left out here
(public repo); the full table with them is in fall26-data .

| Question | Type | Forecast on | Answered (model, seconds) | Failed attempts | Longest Flash attempt | Backup | Research | Reading | Current value (numeric) | Official data | Shadows (Nemotron) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 45516 | discrete | 27 Sep | 3 x 3.8-flash (~15-25 s each) | 3 (503) | - | - | AskNews 402, no research | - | - | - | - |
| 45847 NZ election: Labour-National seat margin | discrete | 28 Sep | emergency: 3.1-flash-lite 37.3 s | 1 | - | flash-lite | AskNews 8 in 1 calls, free 0 | direct | - | - | 2 variants (no shadow model yet) |
| 45844 Trump demolishes Kennedy Center before 2027 | binary | 29 Sep | emergency: nemotron-ultra 132.3 s; emergency: nemotron-ultra 60.9 s | 8 | 0.1 s | window: nemotron | AskNews 8 in 1 calls, free 0 | direct,direct | - | - | 7 variants (ok) |
| 45848 Bundibugyo ebola >100 cases outside DRC in 2026 | binary | 29 Sep | planned: 3.5-flash 51.5 s | 4 | 73.9 s | - | AskNews 23 in 3 calls, free 0 | direct | - | - | 7 variants (ok) |
| 45845 UK cabinet departures before 2027 | multiple_choice | 30 Sep | planned: 3.5-flash 34.5 s | 5 | 132.1 s | - | AskNews 24 in 3 calls, free 0 | direct | - | - | 3 variants (ok) |
| 45868 VIX highest intraday value 1 Oct-24 Dec 2026 | numeric | 30 Sep | planned: 3.5-flash 41.5 s; planned: 3.6-flash 97.7 s | 4 | 105.3 s | - | AskNews 23 in 3 calls, free 0 | direct,direct | 16.04 Units (2026-09-30) | stand-in (fred VIXCLS: daily close, question asks intraday max) | 5 variants (ok, 87.5 s) |

Findings:
- **Flash is the problem:** 4 of the 6 got at least one Flash answer, always from a *later* model in the free
  chain (3.8 on 27 Sep; 3.5 on 29-30 Sep; 3.6 once on 30 Sep 14:12). 45847 and 45844 were saved by the backups
  (Flash-Lite, Nemotron). Most questions ended with a single forecast (the 5-6 planned mostly failed on 503).
- No live Flash attempt took over 6 minutes (longest 132 s, a failed chain). Nemotron answered in 61-132 s.
- Research: AskNews works again (23-24 articles in 3 calls on the last three questions; 402 on 27 Sep).
- Unit check: nothing flagged or dropped. 45868 (VIX intraday max): current value 16.04 read from the
  dossier; FRED VIXCLS shown as a stand-in (daily close vs intraday max), so not used by the unit check.
- Every answer was read directly (no parser call). The follow-up search never triggered (single forecasts).
- To review: 45868's forecast puts about 19% above the upper bound 40 (both Flash models: median 29,
  90th percentile 48, with the VIX at 16). The combining kept what the models said; it's the models' view.
