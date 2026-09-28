"""
Credits readiness, 4a (architect, 29 Sep): pre-flight check of every OpenRouter
model id the credits lineup uses (ensemble.py, primaries and backups), against
OpenRouter's public model list (no key, no model call):
- does the id exist,
- does it take the `reasoning` parameter (we ask for high reasoning),
- its prompt and completion prices (dollars per million tokens).
The free models we use today are checked too (they must cost $0).
Red (exit 1) if any id doesn't exist, or a free model isn't free.

    poetry run python preflight.py
"""
from __future__ import annotations

import os
import sys
from typing import Any

import requests

import ensemble
from bot_config import FREE_MODEL, SHADOW_FORECAST_MODEL

MODELS_URL = "https://openrouter.ai/api/v1/models"


def credits_model_ids() -> list[str]:
    """Every OpenRouter model the credits lineup can call, in a stable order."""
    ids: list[str] = []
    for tier in ensemble.TIERS.values():
        ids += [*tier.binary_round1, *tier.binary_round2]
    for primary, backups in ensemble.BACKUPS.items():
        ids += [primary, *backups]
    ids += list(ensemble.PRICE_CHECK_ONLY)  # price reference only, not used
    return list(dict.fromkeys(ids))


def fetch_models(get=requests.get) -> dict[str, dict]:  # type: ignore[no-untyped-def]
    response = get(MODELS_URL, timeout=30)
    response.raise_for_status()
    return {m["id"]: m for m in response.json().get("data", [])}


def per_million(price: Any) -> float | None:
    try:
        return float(price) * 1_000_000
    except (TypeError, ValueError):
        return None


def check(models: dict[str, dict], ids: list[str], free_ids: list[str]) -> tuple[list[dict], list[str]]:
    """(rows, problems). Model ids may carry LiteLLM's 'openrouter/' prefix."""
    rows, problems = [], []
    for full_id in [*ids, *free_ids]:
        model_id = full_id.removeprefix("openrouter/")
        info = models.get(model_id)
        pricing = (info or {}).get("pricing", {})
        row = {
            "id": model_id,
            "exists": info is not None,
            "reasoning": "reasoning" in ((info or {}).get("supported_parameters") or []),
            "prompt_per_m": per_million(pricing.get("prompt")),
            "completion_per_m": per_million(pricing.get("completion")),
            "context": (info or {}).get("context_length"),
            "free_model": full_id in free_ids,
        }
        rows.append(row)
        if not row["exists"]:
            problems.append(f"{model_id} does not exist on OpenRouter")
        elif row["free_model"] and (row["prompt_per_m"] or row["completion_per_m"]):
            problems.append(f"{model_id} is not free")
    return rows, problems


def table(rows: list[dict]) -> str:
    def money(value: float | None) -> str:
        return "?" if value is None else f"${value:,.2f}"

    lines = [
        "| Model | Exists | Reasoning | Prompt $/M tokens | Completion $/M tokens | Context |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['id']}{' (free, in use now)' if r['free_model'] else ''}"
            f"{' (price reference only, not in the lineup)' if 'openrouter/' + r['id'] in ensemble.PRICE_CHECK_ONLY else ''} | {'yes' if r['exists'] else '**NO**'} "
            f"| {'yes' if r['reasoning'] else 'no'} | {money(r['prompt_per_m'])} | {money(r['completion_per_m'])} "
            f"| {r['context'] or '?'} |"
        )
    return "\n".join(lines)


def main() -> int:
    rows, problems = check(fetch_models(), credits_model_ids(), [SHADOW_FORECAST_MODEL, FREE_MODEL])
    out = "## Credits pre-flight (OpenRouter public model list)\n\n" + table(rows) + "\n\n"
    out += ("**Problems:** " + "; ".join(problems)) if problems else "**All model ids exist; free models cost $0.**"
    print(out)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(out + "\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
