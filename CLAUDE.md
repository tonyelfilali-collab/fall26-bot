# CLAUDE.md

Metaculus Fall 2026 forecasting bot (fork of Metaculus/metac-bot-template).
The owner is a novice: an architect plans, the owner relays. Explain everything in plain, short language.

## Rules

- One small pull request per task. Never push to `main`.
- Never show, log or commit secrets.
- The bot runs only on GitHub Actions, never on the owner's computer.
- ZERO SPEND: we never pay for anything. Only use the free credit key Metaculus will send
  (OpenAI, Anthropic, Google models via OpenRouter), the AskNews free tier, and free public services.
  No Perplexity, no paid APIs. One exception: the paid-Flash fallback below (Tony, 30 Sep).
- Until the credit key arrives, `OPENROUTER_API_KEY` is a $0 key: use only `:free` models,
  and only on the bot-testing-area (id 32977).
- OpenRouter: $10 was bought to raise the free limit. Never call a paid model, EXCEPT
  google/gemini-3.6-flash via PAID_FLASH_FALLBACK, within its caps ($0.15/question, $1.00/UTC day).
  Every other paid model stays refused. Never enable top-up. The key limit ($8) is the hard ceiling.
  Keep free-model use under 600 requests a day. If the key's usage (Credit check workflow) is ever
  above what the spend ledger (fall26-data `status/spend.json`) accounts for, or above $1.00 in a
  UTC day, stop everything and tell the architect.
- When a task is done, report in 3-5 plain sentences what was done and paste the proof
  (links to Actions runs).

## Practical notes

- This clone has two remotes (`origin` = our fork, `upstream` = Metaculus). Always pass
  `-R tonyelfilali-collab/fall26-bot` to `gh`, or it may target the Metaculus repo.
- The repo is public: logs must not contain reasoning or forecast values.
- Secrets: `METACULUS_TOKEN`, `OPENROUTER_API_KEY`, `ASKNEWS_API_KEY` (AskNews is one API key,
  not a client ID + secret), `GEMINI_API_KEY` (Google AI Studio free tier, no billing).
- All model choices are in `bot_config.py` (`ACTIVE_LINEUP`). Only models passing
  `bot_config.is_free_model` may be used until the Metaculus credit key arrives (the one exception:
  the paid-Flash fallback above).
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
