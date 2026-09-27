# CLAUDE.md

Metaculus Fall 2026 forecasting bot (fork of Metaculus/metac-bot-template).
The owner is a novice: an architect plans, the owner relays. Explain everything in plain, short language.

## Rules

- One small pull request per task. Never push to `main`.
- Never show, log or commit secrets.
- The bot runs only on GitHub Actions, never on the owner's computer.
- ZERO SPEND: we never pay for anything. Only use the free credit key Metaculus will send
  (OpenAI, Anthropic, Google models via OpenRouter), the AskNews free tier, and free public services.
  No Perplexity, no paid APIs.
- Until the credit key arrives, `OPENROUTER_API_KEY` is a $0 key: use only `:free` models,
  and only on the bot-testing-area (id 32977).
- When a task is done, report in 3-5 plain sentences what was done and paste the proof
  (links to Actions runs).

## Practical notes

- This clone has two remotes (`origin` = our fork, `upstream` = Metaculus). Always pass
  `-R tonyelfilali-collab/fall26-bot` to `gh`, or it may target the Metaculus repo.
- The repo is public: logs must not contain reasoning or forecast values.
- Secrets: `METACULUS_TOKEN`, `OPENROUTER_API_KEY`, `ASKNEWS_API_KEY` (AskNews is one API key,
  not a client ID + secret), `GEMINI_API_KEY` (Google AI Studio free tier, no billing).
- All model choices are in `bot_config.py` (`ACTIVE_LINEUP`). Only models passing
  `bot_config.is_free_model` may be used until the Metaculus credit key arrives.
- The free Gemini key allows 20 requests/day per model, and every attempt counts. Don't rerun
  Test Bot on Gemini casually; use `poetry run pytest` (dummy server) and the Test Bot
  "one binary" option. Daily counts are in `quota/gemini_free.json` in fall26-data.
- Tests: `poetry run pytest -q` (dummy servers, no real calls). The Unit tests workflow runs them
  on every PR.
- Merging: Claude Code merges code PRs only after Tony relays "the architect says merge".
  Notes-only PRs (NOTES.md, PLAN.md, progress log) may be merged straight away.
- Master switch: repository variable `BOT_ENABLED`. Real-question workflows exit at once unless
  it is `true`. Emergency stop = set it to `false`.
- Logs are public: never log research, reasoning or forecast values. Use the `fall26` logger
  (`PUBLIC_LOGGER_NAME`) for our own messages (question id, status, cost only); every other
  logger's text is hidden by `bot_helpers.configure_public_logging`.
- See NOTES.md for library version, secret names, aggregation and model IDs.
