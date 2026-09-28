"""TEMPORARY probe (removed before merge): how to find a MiniBench round via the API."""
from minibench import _metaculus_get

listing = _metaculus_get("projects/tournaments/")
print("list type:", type(listing).__name__)
items = listing.get("results", []) if isinstance(listing, dict) else listing or []
if isinstance(listing, dict):
    print("list keys:", sorted(listing))
if items:
    print("item keys:", sorted(items[0]))
    print("bench/bot names:", [(t.get("id"), t.get("slug")) for t in items if "bench" in f"{t.get('slug')}{t.get('name')}".lower() or "bot" in f"{t.get('slug')}{t.get('name')}".lower()][:20])
    print("max id in list:", max(t.get("id") or 0 for t in items))
d = _metaculus_get("projects/tournaments/minibench/")
print("slug detail keys:", sorted(d) if isinstance(d, dict) else d)
for q in ("projects/tournaments/?search=minibench", "projects/tournaments/?slug=minibench", "projects/?search=minibench"):
    r = _metaculus_get(q)
    n = len(r.get("results", [])) if isinstance(r, dict) else (len(r) if isinstance(r, list) else None)
    print(q, "->", type(r).__name__, n)
for i in range(33115, 33140):
    t = _metaculus_get(f"projects/tournaments/{i}/")
    if isinstance(t, dict):
        print(i, t.get("slug"), "|", t.get("name"), "|", (t.get("start_date") or "")[:10], (t.get("close_date") or "")[:10])
