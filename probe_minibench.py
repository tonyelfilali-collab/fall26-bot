"""TEMPORARY probe (removed before merge): look up tournaments by number, gently."""
import os
import time

import requests

H = {"Authorization": f"Token {os.environ['METACULUS_TOKEN']}"}
for path in ("projects/tournaments/33125/", "projects/tournaments/33121/", "projects/tournaments/33126/",
             "projects/tournaments/33124/", "projects/33125/"):
    r = requests.get(f"https://www.metaculus.com/api/{path}", headers=H, timeout=30)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    info = (body.get("slug"), body.get("name"), body.get("visibility"), body.get("type")) if isinstance(body, dict) else type(body).__name__
    print(path, r.status_code, info, "retry-after:", r.headers.get("retry-after"))
    time.sleep(3)
