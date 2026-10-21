"""
Frontier pre-flight (architect, 21 Oct; PR 2): one tiny real call per Claude
model id the frontier lineup uses (ANTHROPIC_API_KEY, Console credits: about
a cent in all), to confirm each id answers, and the organization's rate limits
from the response headers (anthropic-ratelimit-*: the Console Limits page's
numbers for this key). GPT-5.6 Sol is checked on OpenRouter's public model
list (no call). Numbers and short labels only (public log).

    poetry run python claude_preflight.py
"""
from __future__ import annotations

import os
import sys

import frontier
import preflight

LIMIT_HEADERS = ("requests-limit", "input-tokens-limit", "output-tokens-limit", "tokens-limit")


def check_claude(model: str) -> dict:
    import anthropic

    row: dict = {"model": model.removeprefix("anthropic/")}
    try:
        client = anthropic.Anthropic(max_retries=0, timeout=120)
        raw = client.messages.with_raw_response.create(
            model=row["model"], max_tokens=512, output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Reply with the single word OK."}],
        )
        message = raw.parse()
        row.update(ok=True, model_answered=message.model, stop=message.stop_reason,
                   cost=frontier.claude_cost(model, message.usage),
                   limits={h: raw.headers.get(f"anthropic-ratelimit-{h}") for h in LIMIT_HEADERS})
    except anthropic.APIStatusError as e:
        row.update(ok=False, error=f"HTTP {e.status_code} {type(e).__name__}",
                   credit=frontier.is_credit_error(e))
    except anthropic.APIError as e:
        row.update(ok=False, error=type(e).__name__)
    return row


def main() -> int:
    rows = [check_claude(m) for m in frontier.CLAUDE_PRICES]
    lines = ["## Frontier pre-flight", "", "### Claude (one tiny call each, ANTHROPIC_API_KEY)", "",
             "| Model id | Answers | Model that answered | Stop | Cost | Requests/min | Input tokens/min | Output tokens/min |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if r["ok"]:
            limits = r["limits"]
            lines.append(f"| {r['model']} | yes | {r['model_answered']} | {r['stop']} | ${r['cost']:.4f} | "
                         f"{limits['requests-limit'] or '?'} | {limits['input-tokens-limit'] or limits['tokens-limit'] or '?'} | "
                         f"{limits['output-tokens-limit'] or '?'} |")
        else:
            lines.append(f"| {r['model']} | **NO** ({r['error']}{'; credit balance too low' if r.get('credit') else ''}) | - | - | - | - | - | - |")
    lines += ["", f"Total: ${sum(r.get('cost', 0.0) for r in rows):.4f}", ""]
    or_rows, problems = preflight.check(preflight.fetch_models(), [frontier.GPT_SOL, frontier.bot_config.PAID_FLASH_MODEL], [])
    lines += ["### OpenRouter (public model list, no call)", "", preflight.table(or_rows), ""]
    problems += [f"{r['model']} did not answer" for r in rows if not r["ok"]]
    lines.append(("**Problems:** " + "; ".join(problems)) if problems else "**Every model id answers / exists.**")
    text = "\n".join(lines)
    print(text)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
