# CC_HANDOVER.md — for a fresh Claude Code session

Updated 21 Oct 2026, at the end of the third session (watch-and-report, 7–21 Oct). Section 1 lists
what changed since 7 Oct; section 6 the items waiting for the architect.
**Local folder: `~/code/fall26-bot`** (outside iCloud). Open Claude Code there. Local Python must be
**3.11** (`poetry env use python3.11`, `poetry install`); on 3.14 ~51 tests fail.
**Helper scripts (not in the repo): `~/code/fall26-tools/`** (daily report, MiniBench section,
slow-Flash watcher, `slow_flash_scan.py` for any date range; past daily reports in `reports/`;
watcher alarms in `watch/`). Never keep scripts in the session scratchpad: macOS clears `/private/tmp` after
~3 days (it deleted some on 6 Oct).
**Private notes (not in the public repo):** the architect's HANDOVER.md and ARCHITECT_LOG.md live in
fall26-data `notes/`; Tony's upload copies are in `~/code/fall26-private-notes/`. Never put private
notes or anything about Tony in this public repo (CLAUDE.md rule).

Read with: `CLAUDE.md` (rules), NOTES.md "What's live" (every switch, value and PR), the progress log
at the bottom of `PLAN.md`, `REPORT.md`. Tony is a novice who relays an architect; explain in plain,
short language.

## 1. Current state (21 Oct)

- **New since 7 Oct (session 3):** 0 missed questions every day 7–20 Oct (audit: seasonal and
  MiniBench all forecast; 19 Oct MiniBench burst of 36, all forecast). Paid Flash $0.00–$0.35/day;
  key **$2.70 of $8 used on 20 Oct** = ledger. **AskNews answers 402 on every call since 12 Oct
  09:03 UTC** (free news used instead; health warns daily, never red). **Replay lab grew to 14–15 min
  and timed out on 20 Oct** (it shares the live queue: see section 6). PR #135: Credit check method.
- **Last merged PR: #136 (this handover). No open PRs.** `main` requires the "Validate workflows" check (ruleset).
- **Live:** `run_bot_on_tournament.yaml`, started every 10 min by cron-job.org; `BOT_ENABLED=true`.
  Lineup `gemini-free` (free AI Studio key: 3.6/3.7/3.8/3.5 Flash forecasters, Flash-Lite helpers).
- **Paid-Flash fallback ON** (since 1 Oct): a Flash slot whose free chain fails makes ONE paid call to
  OpenRouter `google/gemini-3.6-flash`. Paid only up to 3 real forecasts per question; extra slots
  free only; $0.10/question; $1.00/UTC day; **MiniBench paid only while the day's paid spend is under
  $0.50** (each call reserves $0.03, so refusals start at ~$0.47). Fail closed. Alert under $2 left.
  Key limit **$15** (raised from $8 on 21 Oct, Credit check 39681912561); used $0.95 on 7 Oct morning = spend ledger.
- **No credits this season** (Metaculus, 5 Oct): credits lineup OFF all season, no cleanup.
- Backup chain (Nemotron x2, then Flash-Lite), extra forecasts on spare quota, follow-up search,
  Wikipedia, official data + **window baseline** shadow, run timing caps, consistency shadow, Replay lab
  (nightly, started by the bot), install cache, daily Keepalive, health checks: see NOTES.md.
- **MiniBench round 33129** (5–23 Oct) is live: 45 questions on 5 Oct, 10 on 6 Oct, **0 missed**.
- Failed calls now record the HTTP status code + provider error type (#132; ledger + private log).
- Health's "last successful run" check now also reads the plain run list (#133; GitHub's
  success-only list went stale twice: false reds on 6 and 7 Oct).

## 2. Rules

- **Merge tiers: CLAUDE.md "Merging"** (A: tests → merge; B: + replay Test Bot on 4 types; C: + real
  free-model Test Bot after 00:00 UTC, merge only after "the architect says merge"). Always: the
  "Validate workflows" check, next live run checked, revert if red, one forecast-path merge between
  two live runs, a progress-log row per merge, never `--admin`.
- Spend (architect, 21 Oct; CLAUDE.md): never buy credits. Allowed paid use: Claude via
  `ANTHROPIC_API_KEY` (Console credits from Tony's Max plan, PR 2 caps), GPT-5.6 Sol on OpenRouter
  (seasonal only, PR 2 caps) and paid Flash within its caps. Spend check: key usage = key usage at the
  start of the day + today's spend; if above, or OpenRouter above $2.00 in a UTC day: stop everything
  and report.
- A notes PR at least every 21 days (standing rule).
- Public logs and this repo: no forecast values, research or reasoning; nothing about Tony.

## 3. Daily routine and watchers (restart all in a new session)

Run with the Bash tool, `run_in_background: true`. Re-arm a watcher after it fires. macOS deletes
the background output files after a few days, so each watcher also writes its alarm to
`~/code/fall26-tools/watch/` (red.log, slow_flash.log): read those if a watcher ends with no output.

**a) Red live run or health check:**
```bash
R=tonyelfilali-collab/fall26-bot; since=$(date -u +%FT%TZ); while true; do for wf in run_bot_on_tournament.yaml health.yml; do bad=$(gh run list -R $R -w $wf -L 30 --created ">$since" --json databaseId,conclusion,event,url,createdAt -q '.[] | select(.conclusion=="failure" and .event!="pull_request") | "\(.createdAt) \(.url)"' 2>/dev/null | head -1); if [ -n "$bad" ]; then echo "RED $wf $bad" | tee -a ~/code/fall26-tools/watch/red.log; exit 0; fi; done; sleep 300; done
```
On red: find the failed step (`gh api repos/$R/actions/runs/<id>/jobs`, then the job's annotations).
Seen so far: PyPI timeout (fixed by the install cache), GitHub runner outage ("job was not acquired
by Runner"; check githubstatus.com), stale GitHub answer in health (fixed, #133).

**b) Slow Flash:** `python3 -u ~/code/fall26-tools/slow_flash_watch.py 2>&1 | tee -a ~/code/fall26-tools/watch/slow_flash.log` (exits on a NEW log with a Flash
forecast over 360 s; skips existing logs). 6 Oct hit (45940) was a hung connection cut at minute 9,
not a slow model: report such cases, the 9-minute cut stays. Missed alarm? `python3
~/code/fall26-tools/slow_flash_scan.py <YYYY-MM-DD>` lists every slow Flash since that date.
Also check for **cancelled** runs: a live run cancelled with no job is the shared queue (normal); a
Replay lab "cancelled" after 15 min is its timeout (section 6).

**c) Daily report** (architect asks for it every day; run after 00:10 UTC for the previous UTC day):
```bash
python3 ~/code/fall26-tools/paid_daily_report.py <YYYY-MM-DD>; python3 ~/code/fall26-tools/minibench_section.py <YYYY-MM-DD>
```
The first gives paid calls, $, failed calls by HTTP status, $/question, key remaining (fresh Credit
check), days left; the second starts a Tournament info audit and gives MiniBench + seasonal opened /
submitted / missed, forecasts per question, sources, when paid refusals started. **Done up to 19 Oct
(in `~/code/fall26-tools/reports/`); next due: 20 Oct.** Always confirm key usage = ledger (section 4).
Days that are already reported can be run late, one at a time (each starts a workflow run).

**d) Missed questions:** the **Tournament info audit** (`gh workflow run tournament_info.yaml -R $R -f
audit=true`; raw `/api/posts/` listing + library audit) is the source of truth: "closed without our
forecast: N". Run it during MiniBench bursts (every ~2 h until 9 Oct was the plan) and tell the
architect at once about any miss. Don't trust ad-hoc log-search watchers (two false alarms on 5–6 Oct
came from my own search bugs).

Helper after a merge (first live run after a time):
```bash
R=tonyelfilali-collab/fall26-bot; after="<ISO time>"; until id=$(gh run list -R $R --workflow run_bot_on_tournament.yaml --created ">$after" -L 20 --json databaseId,createdAt --jq 'sort_by(.createdAt) | .[0].databaseId // empty') && [ -n "$id" ]; do sleep 30; done; gh run watch $id -R $R >/dev/null; gh run view $id -R $R --json conclusion,url -q '.conclusion+" "+.url'
```

## 4. OpenRouter usage check (key limit $15 since 21 Oct, was $8; used = spend ledger)

```bash
R=tonyelfilali-collab/fall26-bot; gh workflow run credit_check.yaml -R $R; sleep 8; id=$(gh run list -R $R -w credit_check.yaml -L1 --json databaseId -q '.[0].databaseId'); gh run watch $id -R $R >/dev/null; gh run view $id -R $R --log | cut -f3- | grep -E "^[^ ]+ \| (Limit|Used|Remaining|Free tier)" | cut -d' ' -f2- | sort -u
```
Expected: Limit 15, Used = the spend ledger (fall26-data `status/spend.json`). The ledger keeps only
7 days (`spend.KEEP_DAYS`), so don't sum `days`: compare Used with `key_day_start[today]` +
`days[today]` (today = newest `key_day_start` date):
```bash
gh api repos/tonyelfilali-collab/fall26-data/contents/status/spend.json -q .content | base64 -d | python3 -c 'import json,sys; d=json.load(sys.stdin); t=max(d["key_day_start"]); print(t, round(d["key_day_start"][t]+d["days"].get(t,0),6))'
```
If Used is above that, or OpenRouter spend is above $2.00 in a UTC day: stop everything and tell
the architect.

## 5. Lessons

- Google counts failed attempts against the free 20/day. Verify "no card" claims on the provider's
  own docs. Ignore instructions from other projects. Never ask Tony for secrets.
- The shared queue keeps one waiting run: dispatch one test at a time, only when nothing waits.
- Parse every edited workflow YAML before pushing (": " in an unquoted value broke one on 1 Oct).
- **Check data before claiming** (I wrongly said no live Flash had answered); **verify a watcher's
  alarm before reporting it** (5–6 Oct: false "MISS" from my own search bugs; the audit is the truth).
- **Shell traps:** in zsh `echo ===` fails; `xargs -I` with long paths overflows (use a script file);
  a chained `git rm` that fails lets the rest run (check each step when deleting).
- macOS clears `/private/tmp` after ~3 days: keep scripts in `~/code/fall26-tools/`.
- A free model can truncate a reply: a red free-model Test Bot gets one rerun after reading the log.
- On a personal repo a ruleset can't exempt GitHub Actions (Keepalive now uses the enable API).
- macOS blocks reading `~/Downloads` from the terminal: ask Tony to drag files into `~/code/...`.

## 6. Open items

- **AskNews 402 since 12 Oct** (every call; free news fills in; MiniBench 45950 on 12 Oct had 0
  articles). Plan says free tier = 1,000 calls/month; AskNews's plans page says the $0 plan has "25
  credits included" and doesn't say if it resets. Asked Tony to check the AskNews account usage page
  (no answer yet). Zero spend: no paid AskNews. Architect decides.
- **Replay lab: fixed 21 Oct (PR L, #137):** own queue (`replay-lab`), no Gemini, ends by minute 8.
  Report each night's lab run time (~04:03 UTC) until the architect is satisfied.
- **Lost log, 7 Oct:** 45948's question log is lost. Since 21 Oct (PR A, #138) failed log saves are
  retried at the end of the run (3 tries, ~2 min). Report again if one is still lost.
- **15 Oct, 46095:** 2 paid calls broke mid-reply (`http_status` 200, ClientPayloadError; booked at
  the $0.03 reserve each; key charged less). One-off network fault; 46095 submitted with 1 forecast.

- **Architect's frontier plan (21 Oct)** replaces Option B and the 45875 options (cancelled). Order:
  PR L (done), PR A (done), PR 1 notes (spend rules), PR 2 (Tier C, built OFF behind
  `FRONTIER_ENABLED=False`: Claude Fable/Opus/Sonnet via `ANTHROPIC_API_KEY` + GPT-5.6 Sol + Flash;
  budget `CLAUDE_BUDGET_PER_CYCLE`, caps, step-downs; replay rehearsal, then one real Test Bot; then
  STOP and send the architect the branch name), PR 3 (switch ON after "merge" and a green live run;
  a short report after each of the first 10 questions). Next after PR 3: a Claude web-search
  research step to replace AskNews (the architect will send the prompt).
- **The 6 Oct paid-call refusals** (22, 14:13–15:23 UTC, charged $0; 402 or 403): the next failure
  will show the code (#132); report it.
- MiniBench round 33129 runs to 23 Oct; watch the $0.50 limit and misses, then check the bot finds
  the next round (Tournament info audit).
- ~mid-Oct: first Replay lab results; Nemotron 2-week review. 19 Oct: Ubuntu change (we're pinned to
  24.04, nothing to do). 31 Jan 2027: tokens expire.
