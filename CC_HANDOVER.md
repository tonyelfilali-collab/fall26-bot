# CC_HANDOVER.md — for a fresh Claude Code session

Updated 7 Oct 2026, ~12:40 UTC, at the end of the second build session (29 Sep – 7 Oct).
**Local folder: `~/code/fall26-bot`** (outside iCloud). Open Claude Code there. Local Python must be
**3.11** (`poetry env use python3.11`, `poetry install`); on 3.14 ~51 tests fail.
**Helper scripts (not in the repo): `~/code/fall26-tools/`** (daily report, MiniBench section,
slow-Flash watcher). Never keep scripts in the session scratchpad: macOS clears `/private/tmp` after
~3 days (it deleted some on 6 Oct).
**Private notes (not in the public repo):** the architect's HANDOVER.md and ARCHITECT_LOG.md live in
fall26-data `notes/`; Tony's upload copies are in `~/code/fall26-private-notes/`. Never put private
notes or anything about Tony in this public repo (CLAUDE.md rule).

Read with: `CLAUDE.md` (rules), NOTES.md "What's live" (every switch, value and PR), the progress log
at the bottom of `PLAN.md`, `REPORT.md`. Tony is a novice who relays an architect; explain in plain,
short language.

## 1. Current state (7 Oct)

- **Last merged PR: #133. No open PRs.** `main` requires the "Validate workflows" check (ruleset).
- **Live:** `run_bot_on_tournament.yaml`, started every 10 min by cron-job.org; `BOT_ENABLED=true`.
  Lineup `gemini-free` (free AI Studio key: 3.6/3.7/3.8/3.5 Flash forecasters, Flash-Lite helpers).
- **Paid-Flash fallback ON** (since 1 Oct): a Flash slot whose free chain fails makes ONE paid call to
  OpenRouter `google/gemini-3.6-flash`. Paid only up to 3 real forecasts per question; extra slots
  free only; $0.10/question; $1.00/UTC day; **MiniBench paid only while the day's paid spend is under
  $0.50** (each call reserves $0.03, so refusals start at ~$0.47). Fail closed. Alert under $2 left.
  Key limit **$8**; used $0.95 on 7 Oct morning = spend ledger.
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
- Zero spend except paid Flash within its caps; if key usage is ever above the spend ledger or above
  $1.00 in a UTC day: stop everything and report.
- A notes PR at least every 21 days (standing rule).
- Public logs and this repo: no forecast values, research or reasoning; nothing about Tony.

## 3. Daily routine and watchers (restart all in a new session)

Run with the Bash tool, `run_in_background: true`. Re-arm a watcher after it fires.

**a) Red live run or health check:**
```bash
R=tonyelfilali-collab/fall26-bot; since=$(date -u +%FT%TZ); while true; do for wf in run_bot_on_tournament.yaml health.yml; do bad=$(gh run list -R $R -w $wf -L 30 --created ">$since" --json databaseId,conclusion,event,url,createdAt -q '.[] | select(.conclusion=="failure" and .event!="pull_request") | "\(.createdAt) \(.url)"' 2>/dev/null | head -1); if [ -n "$bad" ]; then echo "RED $wf $bad"; exit 0; fi; done; sleep 300; done
```
On red: find the failed step (`gh api repos/$R/actions/runs/<id>/jobs`, then the job's annotations).
Seen so far: PyPI timeout (fixed by the install cache), GitHub runner outage ("job was not acquired
by Runner"; check githubstatus.com), stale GitHub answer in health (fixed, #133).

**b) Slow Flash:** `python3 ~/code/fall26-tools/slow_flash_watch.py` (exits on a NEW log with a Flash
forecast over 360 s; skips existing logs). 6 Oct hit (45940) was a hung connection cut at minute 9,
not a slow model: report such cases, the 9-minute cut stays.

**c) Daily report** (architect asks for it every day; run after 00:10 UTC for the previous UTC day):
```bash
python3 ~/code/fall26-tools/paid_daily_report.py <YYYY-MM-DD>; python3 ~/code/fall26-tools/minibench_section.py <YYYY-MM-DD>
```
The first gives paid calls, $, failed calls by HTTP status, $/question, key remaining (fresh Credit
check), days left; the second starts a Tournament info audit and gives MiniBench + seasonal opened /
submitted / missed, forecasts per question, sources, when paid refusals started. **Next due: the 7 Oct
report (after 00:10 UTC 8 Oct).** Always confirm key usage = ledger.

**d) Missed questions:** the **Tournament info audit** (`gh workflow run tournament_info.yaml -R $R -f
audit=true`; raw `/api/posts/` listing + library audit) is the source of truth: "closed without our
forecast: N". Run it during MiniBench bursts (every ~2 h until 9 Oct was the plan) and tell the
architect at once about any miss. Don't trust ad-hoc log-search watchers (two false alarms on 5–6 Oct
came from my own search bugs).

Helper after a merge (first live run after a time):
```bash
R=tonyelfilali-collab/fall26-bot; after="<ISO time>"; until id=$(gh run list -R $R --workflow run_bot_on_tournament.yaml --created ">$after" -L 20 --json databaseId,createdAt --jq 'sort_by(.createdAt) | .[0].databaseId // empty') && [ -n "$id" ]; do sleep 30; done; gh run watch $id -R $R >/dev/null; gh run view $id -R $R --json conclusion,url -q '.conclusion+" "+.url'
```

## 4. OpenRouter usage check (key limit $8; used = spend ledger)

```bash
R=tonyelfilali-collab/fall26-bot; gh workflow run credit_check.yaml -R $R; sleep 8; id=$(gh run list -R $R -w credit_check.yaml -L1 --json databaseId -q '.[0].databaseId'); gh run watch $id -R $R >/dev/null; gh run view $id -R $R --log | cut -f3- | grep -E "^[^ ]+ \| (Limit|Used|Remaining|Free tier)" | cut -d' ' -f2- | sort -u
```
Expected: Limit 8, Used = the spend ledger total (fall26-data `status/spend.json`, sum of `days`).
If Used is above the ledger: stop everything and tell the architect.

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

- **Architect's "PR 2 of Option B":** never received; Option B / PR 1 not known here. Options sent on
  6 Oct for seasonal 45875 (no paid Flash when all free quota is gone): (a) paid-only slots when free
  quota is gone, (b) quick forecast keeps its paid call, (c) part of the free reserve for seasonal
  only. Wait for the instruction; keep one forecast-path merge between two live runs.
- **The 6 Oct paid-call refusals** (22, 14:13–15:23 UTC, charged $0; 402 or 403): the next failure
  will show the code (#132); report it.
- MiniBench round 33129 runs to 23 Oct; watch the $0.50 limit and misses.
- ~mid-Oct: first Replay lab results; Nemotron 2-week review. 19 Oct: Ubuntu change (we're pinned to
  24.04, nothing to do). 31 Jan 2027: tokens expire.
