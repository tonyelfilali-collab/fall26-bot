# CLAUDE.md

Metaculus Fall 2026 forecasting bot (fork of Metaculus/metac-bot-template).
The owner is a novice: an architect plans, the owner relays. Explain everything in plain, short language.

## Rules

- One small pull request per task. Never push to `main`.
- Never show, log or commit secrets.
- The bot runs only on GitHub Actions, never on the owner's computer.
- ZERO SPEND: we never buy anything. Only use: the Anthropic Console credits that come with Tony's
  Max plan (`ANTHROPIC_API_KEY`), the OpenRouter balance already bought, the free Gemini key, the
  AskNews free tier, and free public services. No Perplexity, no other paid APIs. Never buy credits,
  never turn on auto-reload or top-up (Anthropic or OpenRouter). No Metaculus credits this season.
- Anthropic (architect, 21 Oct): Claude models via `ANTHROPIC_API_KEY` (Console org, workspace
  "fall26-bot", no card, no auto-reload) are allowed, within the frontier caps (#140): `CLAUDE_BUDGET_PER_CYCLE`
  ($200 per cycle (architect, 15 Nov; was $170); cycles run 18th 00:00 UTC -> 18th (architect, 16 Nov: old credit $39.58 left, expires 18 Nov 00:00 UTC; new $200 granted 15 Nov, expires 18 Dec 00:00 UTC; auto-reload stays OFF)), $20 per UTC day (two seasonal agents; $15 with one; 17 Nov: in the last 48 hours of a cycle the day cap is max($20, today's Claude spend + the cycle's dollars left), MiniBench limit = that cap minus one seasonal reservation), MiniBench Claude only while the day's Claude spend
  (running calls included) is under the day cap minus one seasonal question's reservation ($14.40 with Fable 5.2 + the seasonal plain Opus, 16 Nov; $14.70 with Fable only, 12 Nov; $15.40 without; $11.70 with one
  agent; 3 Nov, was $9); the MiniBench agent only under $5 of MiniBench's own day spend and $10 of the whole day's
  (burst days, 2-3 Nov; plain Opus after; 16 Nov: not in the last 48 hours of a cycle, where the agent + Opus + Fable run until the MiniBench day limit), $1.50 per seasonal question, $0.60 per MiniBench question
  ($3.60 / $1.60 while the agent runs, $5.30 seasonal with two agents, + $0.36 for the seasonal plain Opus (16 Nov: seasonal = agent + agent2 + Fable + plain Opus + Flash x2, full mode); the agent is ON; each + $0.72 while plain
  Fable 5.2 runs, 12 Nov: full mode only, an extra voice, no backup), step-downs at $12 / $5 of the cycle left (12 Nov; were 25% / 10%) only while more than 48 hours of the cycle remain (15 Nov: in the last 48 hours full until the credit runs out), pacing: over the line ($200 x share of the cycle gone) + $10
  MiniBench drops the agent, + $20 seasonal also drops agent2. The credits
  expire each billing cycle; when used up, calls fail with "credit balance is too low" and are never
  charged. Replay lab Claude spend (`status/claude_lab_spend.json`, written by the lab only)
  counts in the cycle spend and pacing (14 Nov). Never retry a credit error: after one, Claude is off for 1 hour, then ONE tiny
  claude-haiku-5-5 test call (max_tokens 5) per hour; when it answers, Claude is back on (architect, 14 Nov).
- OpenRouter: paid models allowed only: google/gemini-3.6-flash via PAID_FLASH_FALLBACK, within its
  caps ($0.10/question, $1.00/UTC day, MiniBench paid too (its limit $1.00 since 3 Nov, was $0.50),
  paid only until a question has 3 real forecasts; 17 Nov: only when Claude can't help: no Claude forecast
  planned (then ONE paid call before Nemotron even with every free Flash at 0), or every Claude slot finished
  with nothing readable, or closing within 45 min); openai/gpt-6.1-sol as the GPT shadow only,
  $0.25/question, $1.50/UTC day (ON 22-28 Oct, OFF since 28 Oct; never submitted); moonshotai/kimi-k3 as the Kimi
  shadow only, the same caps (one pinned provider; ON 28 Oct-17 Nov, OFF since 17 Nov; never submitted). Every other paid model stays refused. The key
  limit is the hard ceiling: $10 since 15 Nov (architect: a new key on Tony's other account, which holds $10 of credit;
  OPENROUTER_API_KEY changed 15 Nov 23:31:40 UTC; was $25 on the old key). The spend check compares the new key from
  that point (`status/spend.json` `key_changes`; `key_day_start` 16 Nov = $0.00). Keep free-model use under 600 requests a day. GPT-5.6 Sol is OFF
  (`FRONTIER_GPT_ENABLED = False`; if the architect switches it on: seasonal only, $0.30 per seasonal
  question with paid Flash, none when the key has under $2 left).
- Spend check: the key's usage (Credit check workflow) must equal the key usage at the start of the
  day + today's spend in the ledger (fall26-data `status/spend.json`: `key_day_start[today]` +
  `days[today]`). If it is ever above that, or OpenRouter spend is above $3.50 in a UTC day, stop
  everything and tell the architect.
- When a task is done, report in 3-5 plain sentences what was done and paste the proof
  (links to Actions runs).

## Practical notes

- This clone has two remotes (`origin` = our fork, `upstream` = Metaculus). Always pass
  `-R tonyelfilali-collab/fall26-bot` to `gh`, or it may target the Metaculus repo.
- The repo is public: logs must not contain reasoning or forecast values.
- Secrets: `METACULUS_TOKEN`, `OPENROUTER_API_KEY`, `ASKNEWS_API_KEY` (AskNews is one API key,
  not a client ID + secret), `GEMINI_API_KEY` (Google AI Studio free tier, no billing),
  `ANTHROPIC_API_KEY` (Console credits from Tony's Max plan; no card).
- All model choices are in `bot_config.py` (`ACTIVE_LINEUP`). Paid models only as allowed above
  (Claude via `ANTHROPIC_API_KEY`, paid Flash; GPT-5.6 Sol only if switched on); everything else must pass
  `bot_config.is_free_model`.
- The free Gemini key allows 20 requests/day per Flash model (Flash-Lite: 500/day), and every attempt counts. Don't rerun
  Test Bot on Gemini casually; use `poetry run pytest` (dummy server) and the Test Bot
  "one binary" option. Daily counts are in `quota/gemini_free.json` in fall26-data.
- Tests: `poetry run pytest -q` (dummy servers, no real calls). The Unit tests workflow runs them
  on every PR.
- Merging: Claude Code merges PRs itself (Tony no longer merges or relays merges), by tier:
  - **Tier A** (no forecast-path code: scoring, health, bench, notes, workflows the tournament
    run doesn't use): unit tests green → merge. Notes-only PRs merge straight away.
  - **Tier B** (forecast plumbing, no prompt or model change): unit tests + replay Test Bot
    (`lineup=replay`) green on all 4 types → merge.
  - **Tier C** (prompts, models, answer text reading, dossier): unit tests + real free-model
    Test Bot green on all 4 types → report to the architect; merge ONLY after
    "the architect says merge". Run these Test Bots just after 00:00 UTC (OpenRouter reset).
  - Always: state the tier in the PR description; check the next live cron run after each
    merge; revert at once if it's red; stop an item after 2 failures; add a progress-log row.
  - Never merge more than one forecast-path PR between two live runs.
- Standing rule: a small notes PR (e.g. a progress-log row) merges into `main` at least every 21 days,
  so GitHub never pauses the workflows. Health warns when main's last commit is older than 21 days
  and goes red if any workflow isn't "active"; Keepalive re-enables every workflow daily.
- `main` requires the "Validate workflows" check (ruleset; YAML parse + actionlint on every PR). Never
  merge with `--admin` to get around it.
- Master switch: repository variable `BOT_ENABLED`. Real-question workflows exit at once unless
  it is `true`. Emergency stop = set it to `false`.
- Logs are public: never log research, reasoning or forecast values. Use the `fall26` logger
  (`PUBLIC_LOGGER_NAME`) for our own messages (question id, status, cost only); every other
  logger's text is hidden by `bot_helpers.configure_public_logging`.
- See NOTES.md for library version, secret names, aggregation and model IDs.
