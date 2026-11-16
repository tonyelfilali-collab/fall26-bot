# CC_HANDOVER.md — for a fresh Claude Code session

Updated **16 Nov 2026, ~16:00 UTC** (end of the session that ran 14–16 Nov). A new session should be
able to pick up from this file alone. Read it with: `CLAUDE.md` (the rules; they override this file),
NOTES.md "What's live" (every switch, value and PR), the progress log at the bottom of `PLAN.md`.

- **Local folder: `~/code/fall26-bot`** (outside iCloud). Local Python must be **3.11**
  (`poetry env use python3.11`, `poetry install`). Tests: `poetry run pytest -q` (~1,080, no network).
- **Helper scripts (not in the repo): `~/code/fall26-tools/`** (old daily-report scripts, watcher
  alarm files in `watch/`, past reports in `reports/`). Keep scripts there, never in the session
  scratchpad (macOS clears `/private/tmp` after ~3 days).
- **Private notes** (architect's notes, reviews, scores, forecasts) live only in the private repo
  `tonyelfilali-collab/fall26-data`. This repo is public: no research, forecasts, reasoning,
  secrets or private notes here or in any log.
- The owner is a novice who relays an architect. Plain, short language; give a paste-ready message
  for the architect with every report. Always pass `-R tonyelfilali-collab/fall26-bot` to `gh`.

## 1. What's live (16 Nov)

- **Live workflow:** `run_bot_on_tournament.yaml`, started every 10 min at **:x2** by **cron-job.org**
  (workflow_dispatch); GitHub's own schedule is only a backup. Master switch: repo variable
  `BOT_ENABLED=true` (emergency stop = `false`). Seasonal and MiniBench in one run queue.
- **MiniBench round 33169** (16 Nov – 4 Dec, found by the slug `minibench`).
- **Lineup:** `ACTIVE_LINEUP = "gemini-free"` with `FRONTIER_ENABLED = True` (frontier.py):
  - Seasonal: Claude agent (Opus 5.5 + web search/fetch) + agent2 + plain Fable 5.2 + Flash x2.
    **Pending (#212):** + plain Opus 5.5 (see section 3).
  - MiniBench: agent + plain Opus + Fable + Flash x2 (agent only within the MiniBench rules below).
  - GPT-5.6 Sol: OFF (`FRONTIER_GPT_ENABLED = False`).
- **Aggregation: `AGGREGATION_MODE = "claude_first"`**, `CLAUDE_FIRST_MIN = 1` (#211): when 1+ Claude
  forecasts were read, only the Claude forecasts are combined (one Claude forecast is submitted alone);
  Flash is used only when there is no Claude forecast. Haiku never counts as a Claude forecast.
- **Claude caps (frontier.py):** $20 per UTC day with two seasonal agents; per question $1.50 seasonal
  / $0.60 MiniBench, raised while the agent runs ($5.30 seasonal with two agents, $1.60 MiniBench),
  each + $0.72 while plain Fable runs (full mode only). MiniBench day limit = $20 − one seasonal
  question's reservation ($14.70 with Fable; $14.40 once #212 is merged).
  - MiniBench agent: only under $5 of MiniBench's own day spend and $10 of the whole day's;
    **lifted in the last 48 hours of a cycle** (#210).
  - Step-downs: "low" under $12 of the cycle left, "minimal" under $5, **only while more than 48
    hours of the cycle remain**. In the last 48 hours: full until the credit runs out (#199).
  - Pacing: over the line ($200 × share of the cycle gone) + $10 → MiniBench drops the agent;
    + $20 → seasonal also drops agent2.
- **Nemotron-only hold (#216):** if only the free Nemotron backup answered and the question closes after
  the next 00:30 UTC, nothing is submitted; a later run retries. Inside 45 min of the close every run
  still submits whatever it has (never miss).
- **Dossier:** official data line (FRED / CoinGecko / Yahoo; Yahoo answers 429) + **typical change line**
  (#217: "Typical change over <horizon>: 10-90% range of past moves = …", horizon = latest data date →
  resolution). Free news (AskNews is 402, see section 6), Wikipedia, follow-up search.
- **Paid OpenRouter:** paid Flash fallback ON (`google/gemini-3.6-flash`, $0.10/question, $1.00/UTC day,
  MiniBench $1.00); Kimi shadow ON (`moonshotai/kimi-k3`, pinned `relace/fp4`, $0.25/question,
  $1.50/day, never submitted); GPT shadow OFF.
- **Free shadows (never submitted):** Nemotron shadow forecaster, stretch 1.2 / 1.5, mean variants,
  window baseline, window blend, consistency, referee (Opus via the Claude budget).
- **Queue watchdog** (`queue_watchdog.yaml`, every 10 min at **:x0** by cron-job.org, GitHub schedule
  as backup): cancels a never-started live run after 10 min; since #218 it force-cancels when GitHub
  refuses the plain cancel, and logs HTTP codes. Opens one alert issue a day when it acts.
- **Other automation:** daily health check (started by the first live run after 06:00 UTC), Keepalive
  05:00 UTC, Replay lab (~05:00 UTC, started by the bot, own queue), Scoreboard (Mon 06:30 UTC;
  daily 06:45 schedule check; GitHub often runs these hours late), Unit tests + "Validate workflows"
  on every PR (required check on `main`).

## 2. Money

### Claude (Anthropic Console, `ANTHROPIC_API_KEY`, workspace "fall26-bot", no card, auto-reload OFF)
- **Cycles run 18th 00:00 UTC → 18th** (`CLAUDE_CYCLE_RESET_DATE = "2026-11-18"`), **$200 per cycle**
  (`CLAUDE_BUDGET_PER_CYCLE`). Two credits overlap now: the old one ($39.58 left on 16 Nov, expires
  18 Nov 00:00 UTC) and a new $200 (granted 15 Nov, expires 18 Dec 00:00 UTC).
- **We are in the last 48 hours of the 18 Oct – 18 Nov cycle:** mode "full", ~$27 of the cycle
  budget left at 15:32 UTC 16 Nov (16 Nov spend $13.74 by mid-afternoon).
- Ledgers (fall26-data): `status/claude_spend.json` (live: days, cycles, minibench_days,
  credit_off, credit_errors) and `status/claude_lab_spend.json` (Replay lab, written only by the lab;
  counts in the cycle spend and pacing).
- **Credit error rule:** never retry a "credit balance is too low" error. After one, Claude is off for
  1 hour, then ONE tiny `claude-haiku-5-5` test call (max_tokens 5) per hour; when it answers, Claude is
  back on. Failed calls are never charged.
- The daily health check shows each day's Claude $, the cycle total and the mode.

### OpenRouter (`OPENROUTER_API_KEY`)
- **New key since 15 Nov 23:31:40 UTC** (the owner's other account, $10 of credit). The ledger note
  is in fall26-data `status/spend.json` `key_changes`; `key_day_start["2026-11-16"] = 0.00`.
  **Key limit: should be $10**; on 16 Nov the key still showed a $25 limit; the owner was asked to
  set it to $10 on openrouter.ai (account credit is the real ceiling either way).
- **Spend check (CLAUDE.md):** Credit check workflow usage = `key_day_start[today]` + `days[today]`.
  If usage is ever above that, or OpenRouter spend is above $3.50 in a UTC day: **stop everything**
  (`BOT_ENABLED=false`) and tell the architect. 16 Nov so far: $1.01.
  ```bash
  gh api repos/tonyelfilali-collab/fall26-data/contents/status/spend.json -q .content | base64 -d | python3 -c 'import json,sys; d=json.load(sys.stdin); t=max(d["key_day_start"]); print(t, round(d["key_day_start"][t]+d["days"].get(t,0),6))'
  ```
- Free models: under 600 requests a day. Free Gemini key: Flash 20/day, Flash-Lite 500/day, every
  attempt counts (don't rerun Gemini Test Bots casually).
- Never buy credits, never turn on auto-reload (Anthropic or OpenRouter). No Metaculus credits.

## 3. Scheduled and pending items (exact times, pass conditions)

1. **#212 — seasonal plain Opus (Tier C, pre-approved by the architect, 16 Nov).** Branch
   `seasonal-plain-opus`, rebased on main, unit tests green (1074), rehearsal 79/79.
   - **At 00:04 UTC 17 Nov** (after the Claude day reset; only when no live run is queued/waiting):
     `gh workflow run test_bot.yaml -R tonyelfilali-collab/fall26-bot --ref seasonal-plain-opus -f lineup=frontier-agents2 -f frontier_path=seasonal`
     (it shares the live queue and charges the live Claude ledger).
   - **Pass:** all 4 seasonal types get agent + agent2 + plain Opus + Fable; all 4 Claude forecasts
     parsed on each; no cap overrun; all 4 submitted.
   - **If green:** merge #212 (pre-approved), check the next live run's lineup line says
     "seasonal plain Opus on" and is green, add a PLAN row, report. If red: read the log, one fix
     at most, then report (stop after 2 failures).
2. **18 Nov 00:00 UTC — cycle switch check.** The first live run after 00:00 UTC 18 Nov should show
   the new cycle: "full ($200.00 of the cycle budget left)" or close to it, not "last 48 hours". Check
   `cycles` in `status/claude_spend.json` gets a `2026-11-18` entry. If Claude hits a credit error
   instead, the 1-hour safety applies; tell the architect (the new $200 credit should cover it).
3. **Monday report (next: Mon 23 Nov, ~06:00 UTC health check).** Health shows standings: rank,
   score, prize shown, and the **top-50 line** (#213: "#50 score / prize; us rank / score / prize;
   gap and its change since last Monday"). 16 Nov: rank 112, score 212.28, prize shown $102.
   Send the architect the line.
4. **claude_first monitor.** Every Scoreboard writes fall26-data `status/claude_first.json`; health
   shows the line. Alert the architect at once if, at 25+ agent-era resolved questions, claude_first
   is worse than "all" by more than 0.05 per question (`aggregation.MONITOR_*`).
5. **Stretch report at 30 (#214).** Every Scoreboard writes `status/stretch_monitor.json` (agent-era
   resolved binary: k = 1.2 / 1.5 vs submitted, logged shadows, leave-one-out). Health warns
   "report due: tell the architect" at 30 questions; then send the private numbers (from the file /
   the private Scoreboard report in fall26-data `scoreboard/`). Monitor only: no live change.
6. **Typical change line, first real use (#217):** confirm on the first live numeric/discrete question
   matched to a data series (log line "typical change line added"); report it.
7. **Numeric review (done 16 Nov):** fall26-data `reviews/numeric_2026-11-16.md`; its two suggestions
   became #216 and #217.
8. **Standing rules:** Kimi re-pin only after 24 h of every call failing (pre-approved: re-pin to
   Relace's current endpoint, replay Test Bot, merge, tell the architect); a notes PR into `main` at
   least every 21 days.

**Open PRs:** #212 only (waiting for item 1). Everything else from 14–16 Nov is merged (#199, #208–
#211, #213, #214, #216, #217, #218). Issue #215 is the watchdog's 16 Nov alert (open; close it once the owner has seen it).

## 4. Watchers and timers (restart them in a new session)

Session timers (CronCreate) die with the session: **re-create the #212 one** if the new session starts
before 00:04 UTC 17 Nov:
- CronCreate, one-shot, `4 0 17 11 *` (local time = UTC on this Mac): "Run the #212 Test Bot as in
  CC_HANDOVER.md section 3 item 1".

Waiting for a run without blocking: Bash with `run_in_background: true`, an until-loop, e.g. the
first live run after a merge:
```bash
R=tonyelfilali-collab/fall26-bot; after="<ISO time of the merge>"; until gh run list -R $R -w run_bot_on_tournament.yaml -L 4 --json createdAt,status --jq ".[] | select(.createdAt > \"$after\" and .status==\"completed\") | .status" | grep -q .; do sleep 30; done; gh run list -R $R -w run_bot_on_tournament.yaml -L 4 --json databaseId,createdAt,conclusion,headSha --jq ".[] | select(.createdAt > \"$after\") | \"\(.databaseId) \(.conclusion) \(.headSha[:7])\""
```
Red live run or health check: check the failed step's annotations (`gh api repos/$R/actions/runs/<id>/jobs`).
A tool call can't `sleep` in a chain; use an until-loop or `run_in_background`.

## 5. Daily routine

- After each live run of interest: `gh run view <id> -R … --log | grep "fall26 - "` (public lines:
  question id, status, cost) and the "Frontier lineup:" line (mode, $ left, agents, Fable).
- Daily: health check result; Claude $ for the day; OpenRouter spend check (section 2); any missed
  question (`gh workflow run tournament_info.yaml -R … -f audit=true` is the source of truth).
- Merge tiers (CLAUDE.md): A tests → merge; B + replay Test Bot on 4 types; C + real free-model
  Test Bot just after 00:00 UTC and "the architect says merge" (unless pre-approved). One
  forecast-path merge between two live runs; check the next live run; PLAN.md row per PR; never `--admin`.
- Test Bot queues: `frontier*`, `gemini-free` and paid-shadow tests share the live `forecast-bot`
  queue (one dispatch at a time, only when nothing is waiting, or a live run gets cancelled);
  `replay*` / rehearsal lineups use `test-bot-no-gemini` (safe any time).

## 6. Known quirks

- **GitHub queued runs:** a live run can sit "queued" with no runner (16 Nov 14:22–15:12; 30 Oct).
  Later runs still go; the watchdog now force-cancels it. GitHub also runs scheduled workflows
  (Scoreboard, health backup) hours late: cron-job.org drives the important ones.
- **AskNews 402** on every call (again since ~03:53 UTC 16 Nov; free tier used up): free news sources
  fill in; architect: no change, no spend.
- **Kimi provider blips:** single `relace/fp4` failures happen (31 Oct); not a reason to re-pin
  (rule in section 3 item 8).
- **Yahoo Finance answers 429:** the official data line skips stocks gracefully.
- Free models sometimes truncate a reply: a red free-model Test Bot gets one rerun after reading the log.
- A PR that edits PLAN.md conflicts with the next one merged: rebase, keep both rows.
- Shell: zsh `echo ===` fails; macOS blocks reading `~/Downloads` from the terminal.
