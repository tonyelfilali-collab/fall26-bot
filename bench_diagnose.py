"""
One-off diagnostic for the test bench (Tier A): why no community predictions?
Uses METACULUS_READ_TOKEN and prints ONLY yes/no answers, HTTP statuses, key
names and the token's username. No forecast values (the log is public).
"""
from __future__ import annotations

import asyncio
import os

import requests

API = "https://www.metaculus.com/api"


def describe(label: str, question_json: dict) -> str:
    aggregations = (question_json or {}).get("aggregations")
    if not isinstance(aggregations, dict):
        return f"{label}: aggregations present: no"
    latest = (aggregations.get("recency_weighted") or {}).get("latest") if isinstance(aggregations.get("recency_weighted"), dict) else None
    return (
        f"{label}: aggregations keys: {sorted(aggregations)}; "
        f"recency_weighted.latest present: {'yes' if latest else 'no'}"
        + (f"; latest keys: {sorted(latest)}" if isinstance(latest, dict) else "")
    )


def main() -> None:
    from bot_helpers import silence_noisy_dependencies

    silence_noisy_dependencies()
    from forecasting_tools import ApiFilter, MetaculusClient

    token = os.environ["METACULUS_READ_TOKEN"]
    headers = {"Authorization": f"Token {token}"}

    me = requests.get(f"{API}/users/me/", headers=headers, timeout=30)
    print(f"1. users/me: HTTP {me.status_code}; username: {me.json().get('username') if me.ok else '?'}")

    listing = requests.get(
        f"{API}/posts/",
        headers=headers,
        params={"statuses": "open", "forecast_type": "binary", "order_by": "-forecasts_count", "limit": 3, "with_cp": "true"},
        timeout=30,
    )
    print(f"2. popular open binary list: HTTP {listing.status_code}")
    for post in (listing.json().get("results", []) if listing.ok else [])[:3]:
        print("   " + describe(f"list post {post['id']}", post.get("question") or {}))
        detail = requests.get(f"{API}/posts/{post['id']}/", headers=headers, params={"with_cp": "true"}, timeout=30)
        print(f"   post {post['id']} detail: HTTP {detail.status_code}")
        if detail.ok:
            print("   " + describe(f"detail post {post['id']}", detail.json().get("question") or {}))

    client = MetaculusClient(token=token)
    found = asyncio.run(
        client.get_questions_matching_filter(
            ApiFilter(allowed_statuses=["open"], allowed_types=["binary"], num_forecasters_gte=30, is_in_main_feed=True, group_question_mode="exclude"),
            num_questions=1, randomly_sample=False, error_if_question_target_missed=False,
        )
    )
    if found:
        q = found[0]
        print("3. " + describe(f"library list post {q.id_of_post}", (q.api_json or {}).get("question") or {}))
        print(f"   library community_prediction_at_access_time set: {'yes' if getattr(q, 'community_prediction_at_access_time', None) is not None else 'no'}")
    else:
        print("3. library list: no question returned")


if __name__ == "__main__":
    main()
