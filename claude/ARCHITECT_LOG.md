# Architect log

Decisions and facts from each architect session, newest at the bottom.

## 28 Sep 2026, evening (architect session 3)
- Handover checked: watchers restarted, OpenRouter $0, runs green.
- AI Studio limits (Tony's screenshot): Flash 20/day; Flash-Lite 500/day (ledger 400, #69); Google counts over-limit attempts. Gemini 2.5 closed to new users -> no Search grounding on this key (probe #70).
- FutureEval leaderboard (score vs GPT-4o): Fable 5 16.1, Opus 5 15.8, Gemini 3.6 Flash 13.2, GPT-5.6 Sol 12.9, 3.5 Flash 12.2, Gemini 3 Flash 6.4, Nemotron Ultra 5.8, Gemma 4 2.5, Flash-Lite 1.9/0.7. 3.7/3.8 Flash unscored (assumed >= 3.6: a guess based on the version trend).
- Decisions: backup chain = Nemotron x2 first, Flash-Lite only if Nemotron gives nothing (#67; my earlier "both together" was wrong). Gemini 3 Flash and Gemma dropped. Test Bot runs without Gemini get their own queue (#71).
- Credits readiness (lineup still OFF): pre-flight (#72), real-price cost table (#73), free first (#74), fail-closed spend guards with key-vs-ledger backstop (#75), switch-on checklist incl. measuring real output tokens after 10 questions (#76 + notes). Fable 5.1 removed; round 2 = one more forecast per family (#79). Full tier ~$0.64/question at the ASSUMED 8k output tokens.
- Consistency shadow for sibling questions (#78), shadow only.
- Open: credits (follow up Mon 5 Oct if no reply); review table after 5 distinct real questions; Nemotron scoring after ~2 weeks.

## 29 Sep 2026 (architect session 3, day)
- Health: self-started by the first tournament run after 06:00 UTC (#84); no second cron needed.
- MiniBench audit: round 33125's 60 questions all closed 24 Sep, before go-live; no miss. New rounds found within one run (#90); burst rehearsal passed (#91), about 2.3 forecasts/question = the free-tier ceiling.
- Live since today, each with an on/off switch: extra forecasts on spare quota (#85; 25/day expected during MiniBench, #89), disagreement follow-up search (#86), Wikipedia (#87). Yahoo and Stooq blocked: stocks not covered; FRED covers indices, FX and oil.
- Replay lab (#92): variants rerun on frozen dossiers after questions resolve (E1-E4). This is the measurement tool for all tuning; results from about mid-October.
- Decision: pause feature building; wait for credits, resolved questions and the MiniBench burst.

