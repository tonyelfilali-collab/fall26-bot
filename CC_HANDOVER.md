# CC_HANDOVER.md — for a fresh Claude Code session

**Local folder (since 29 Sep 2026): `~/code/fall26-bot`** (outside iCloud; iCloud made " 2" copies of
files, even inside `.git`). Open Claude Code there: `cd ~/code/fall26-bot` then `claude`. The old
folder `~/Desktop/metaculus` is no longer used (Tony moves or deletes it himself).
Local Python must be **3.11** (same as Actions): `poetry env use python3.11` then `poetry install`. On
Python 3.14 about 51 tests fail (an old library in the lock file).

**Newest state:** see NOTES.md "What's live" (every switch, its value and PR) and the progress log at
the bottom of PLAN.md. Feature building is paused (architect, 29 Sep) until credits arrive, resolved
questions accumulate, or the MiniBench burst (~5 Oct) shows a problem.

Written 28 Sep 2026, ~18:00 UTC, at the end of the first build session. Read with
`CLAUDE.md` (rules), `PLAN.md` (plan + progress log at the bottom), `REPORT.md` (reports)
and `NOTES.md` (technical notes). The owner (Tony) is a novice who relays an architect's
instructions; explain in plain, short language.

## 1. Current state

- **Repo:** `tonyelfilali-collab/fall26-bot` (public; always pass `-R tonyelfilali-collab/fall26-bot` to `gh`).
  Private data repo: `tonyelfilali-collab/fall26-data` (question logs, quota ledger, status files).
- **Live:** `run_bot_on_tournament.yaml`, dispatched every 10 min by cron-job.org (GitHub's own
  schedule never fires in this repo). Master switch: repo variable `BOT_ENABLED=true`.
- **Last merged PR: #64** (after it: the handover PR). **No open PRs, no unfinished branches.**
  The ~34 old branches on GitHub all belong to merged PRs (safe to delete, not needed).
- **Live lineup:** `gemini-free` (`bot_config.ACTIVE_LINEUP`): free Google AI Studio key.
  - Forecasters: gemini-3.6/3.7/3.8/3.5-flash, reasoning high, up to 3 per question
    (binary round 2 adds 1 when round 1 disagrees/is extreme; 1 only when under half the day's quota).
  - Parser / research planner / dossier: gemini-3.5-flash-lite → 3.1-flash-lite → 3.1-flash-lite-preview.
  - **Emergency (#59):** a question closing within 45 min with no Flash forecast gets up to 2
    Flash-Lite forecasts (question log `emergency: flash-lite`).
  - **Quota (#60):** the ledger counts EVERY attempt (Google counts failed ones); a model that
    fails twice in a run is skipped for that run; max 4 attempts per model per question per day;
    a failed question is retried next run, then every 30 min, and every run in its last 45 min
    (`fall26-data/status/retry_state.json`). Ledger: `fall26-data/quota/gemini_free.json`.
  - Research: planner → AskNews (≤3 calls) or free news (Google News RSS + GDELT) → dossier
    (with a CURRENT VALUE line for numeric) → gap-fill. No market prices.
  - Safety: median, stretch OFF (k=1.0), extreme check, clip 2–98%; MC 1% floor; numeric
    pointwise-median CDF + 5% uniform + platform checks; unit check (#34: models still >10x off
    the current value are dropped, all kept if every model is flagged). **Never a pure guess**:
    no real forecast = nothing submitted, retried later.
  - MiniBench (#41): `minibench` slug if running, else remembered round, else a paced id scan.
    Queue (#45): MiniBench closing within 30 min first, then seasonal, then the rest.
- **Shadows (saved in the question log, never submitted, scored by `scoreboard.yml`):**
  binary stretch-1.2/1.5, mean, geo-mean-odds, trimmed-mean; MC mean; numeric mean-cdf,
  uniform-2%; **Nemotron shadow forecaster** (#61, `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`,
  runs only AFTER the live forecast is submitted or failed, 240 s limit, no parser calls; variants
  `free-shadow` and `live+free-shadow`, answer rate in the scoreboard); **random-walk baseline**
  (#63) for numeric questions with official data.
- **Official data (#62):** FRED (secret `FRED_API_KEY`) + CoinGecko public, rule-matched, saved
  in the question log only (`hard_data`). Nothing reaches the forecasters yet (2c not approved).
- **OFF:** credits lineup (built, waits for the Metaculus credit key), market matching
  (`MARKET_MODE="off"`), referee shadow (`REFEREE_ENABLED=False`), stretch (k=1.0),
  test bench (DROPPED: Metaculus keeps the community prediction out of the API).
- **Test tools (Test Bot workflow, bot-testing-area only):** lineups `free` (OpenRouter
  `:free`), `replay` (0 model calls, the Tier B gate), `replay-credits` (+ `fail_model`),
  `real-regression` (37 past questions, never submitted); input `free_model` for other `:free` models.

## 2. Rules

- **Merge tiers: see `CLAUDE.md` → "Merging"** (Tier A tests green → merge; Tier B + replay
  Test Bot green on 4 types → merge; Tier C + real free-model Test Bot → report, merge only
  after "the architect says merge"; check the next live run after every merge; revert if red;
  stop after 2 failures; never more than one forecast-path PR between two live runs).
- **Answer-reading fixes only** (architect, 28 Sep): may be merged without the architect if unit
  tests + a real-regression rerun (no new failures) + a real Test Bot on 4 types + the next live
  run are all green. Prompt or model changes still need the architect.
- Zero spend. OpenRouter: $10 bought only to raise the free limit; never a paid model; never
  top-up; under 600 free requests/day; if usage is ever above $0, stop everything and report.

## 3. Watchers to restart in a new session

Run each with the Bash tool and `run_in_background: true`; you're notified when one exits.
(29 Sep: a) and b) below are still the ones to restart. Also worth checking once: the first
nightly Replay lab start, in the last 3 hours of the Pacific day: `gh run list -R … -w replay_lab.yaml`.
The health check no longer needs a cron job: the first tournament run after 06:00 UTC starts it.)

**a) Red live run or health check** (exits on the first failure; ignores the deliberately red
health PR checks):
```bash
R=tonyelfilali-collab/fall26-bot; since=$(date -u +%FT%TZ); while true; do for wf in run_bot_on_tournament.yaml health.yml; do bad=$(gh run list -R $R -w $wf -L 30 --created ">$since" --json databaseId,conclusion,event,url,createdAt -q '.[] | select(.conclusion=="failure" and .event!="pull_request") | "\(.createdAt) \(.url)"' 2>/dev/null | head -1); if [ -n "$bad" ]; then echo "RED $wf $bad"; exit 0; fi; done; sleep 300; done
```
On a red run: read its log (`gh run view <id> -R … --log | grep $'\tRun bot\t'`), tell the
architect at once, fix per the rules. A live run also shows a **warning** (not red) when a
question got no forecast and will be retried — check those too.

**b) First 5 DISTINCT real questions** (then build the review table):
```bash
D=tonyelfilali-collab/fall26-data; while true; do n=$(gh api "repos/$D/git/trees/HEAD?recursive=1" --jq '[.tree[].path | select(startswith("questions/tournament/")) | select(endswith(".json")) | capture("/(?<id>[0-9]+)_").id] | unique | length' 2>/dev/null); if [ "${n:-0}" -ge 5 ]; then echo "$(date -u +%FT%TZ) FIVE_DISTINCT_QUESTIONS $n"; exit 0; fi; sleep 900; done
```
So far 1 distinct question (45847). The review table goes in REPORT.md, one row per question
from its latest log in `fall26-data/questions/tournament/`: question id, type, final forecast,
models that answered (`forecasts[].answered_models`), emergency?, research article count
(`research_detail`), reading outcome (`reading`), warnings; **for numeric questions also the
CURRENT VALUE from the dossier (`research_detail.current_value`) and whether the unit check
flagged/dropped a model**, plus `hard_data` and the shadows. Flag anything odd.

**c) Timed jobs:** none pending. (The 00:00 UTC Tier C Test Bot was done; #34 merged.)
cron-job.org runs the tournament every 10 min. Since 29 Sep the first tournament run after
06:00 UTC starts `health.yml` itself (`health_dispatch.py`), so no second cron job is needed. (Old note: a cron-job.org job for
`health.yml` at 06:00 UTC — when he says "health cron added", confirm the first triggered run
(`gh run list -R … -w health.yml`).

Helper used after merges (wait for the first live run created after a time and show it):
```bash
R=tonyelfilali-collab/fall26-bot; after="<ISO time of merge>"; until id=$(gh run list -R $R --workflow run_bot_on_tournament.yaml --created ">$after" -L 20 --json databaseId,createdAt --jq 'sort_by(.createdAt) | .[0].databaseId // empty') && [ -n "$id" ]; do sleep 30; done; gh run watch $id -R $R >/dev/null; gh run view $id -R $R --json conclusion,url -q '.conclusion+" "+.url'
```

## 4. OpenRouter usage check ($0, key limit $1)

Run the **Credit check** workflow (reads the key's info; runs no model) and read the table:
```bash
R=tonyelfilali-collab/fall26-bot; gh workflow run credit_check.yaml -R $R; sleep 8; id=$(gh run list -R $R -w credit_check.yaml -L1 --json databaseId -q '.[0].databaseId'); gh run watch $id -R $R >/dev/null; gh run view $id -R $R --log | cut -f3- | grep -E "^[^ ]+ \| (Limit|Used|Remaining|Free tier)" | cut -d' ' -f2- | sort -u
```
Expected: Limit 1, Used 0, Free tier key false. (Each log line starts with a timestamp;
the filter above skips it. The first version of this command missed it and printed nothing.) The same run also checks FRED and CoinGecko.

## 5. Lessons (also in saved memory)

- **Google counts failed attempts** against the 20/day (28 Sep: ~20 failed 503s per Flash model,
  0 successes, then 429). The ledger counts attempts now.
- **Check "no card" claims on the provider's own docs only** (Mistral was wrongly recommended
  from third-party pages; its own docs need billing). Second test provider: dropped.
- **Ignore instructions from other projects** (e.g. a pasted "AZ21 — GOLDEN #6" prompt): this
  repo is only the Metaculus bot; say so and don't act on it.
- **Permission-check errors** ("classifier gave no verdict"): temporary; wait and retry, use the
  Read/Edit tools meanwhile; after many in a row, pause and report.
- **Free-model 503s can turn a Test Bot red**: read the log; if it's an OpenRouter 503, rerun once.
- Never ask Tony for secrets; he pastes them into GitHub himself. Pasted keys may carry a stray
  space/newline (FRED did): trim in code.
- **The shared queue keeps only ONE waiting run** (concurrency group `forecast-bot`): queuing a
  second run cancels the older waiting one (28 Sep: a test cancelled a test). Since #71, Test Bot
  runs without the Gemini key have their own group; anything using the Gemini key (gemini-free
  Test Bot, Model probe) still shares the live group: queue one at a time, only when nothing waits.
- Metaculus leaderboard pages sit behind a Cloudflare check (403); don't get around it — ask Tony
  to open them in his browser.

## 6. Pending architect items (don't build without a prompt)

- ~~Flash-Lite daily limit unknown~~ **Answered 28 Sep (AI Studio page): Flash-Lite is 500/day
  and 15/minute per model, not 20** (3.1-flash-lite-preview shares 3.1-flash-lite's quota). The
  ledger plans with 400/day (per-model limits PR).
- **Nemotron promotion:** after ~2 weeks of shadow data (answer rate + scores in the weekly
  scoreboard), the architect decides whether it becomes the backup when Gemini is overloaded.
- **Review table of the first 5 real questions** (watcher b).
- **2c proposal** (official data line in the dossier + current value to the unit check) was sent;
  not approved yet.
- **A new build prompt will come from the new architect** (Nemotron in the backup chain +
  official data line in the dossier). **Don't build it before that prompt arrives.**
