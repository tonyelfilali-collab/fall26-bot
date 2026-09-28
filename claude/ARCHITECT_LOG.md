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
