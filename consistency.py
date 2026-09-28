"""
Related-question consistency (architect, 29 Sep): a zero-cost SHADOW, never
submitted, no model calls.

- Sibling questions: binary questions open at the same time (same tournament)
  whose titles are identical except for ONE number or ONE date, e.g.
  "Will X exceed 100 / 200 / 300 by ...?" or "Will X happen by Oct / Nov / Dec?".
- Direction, from the words just before that number or date:
  - "above / exceed / more than / at least / over / ..." a higher threshold
    must never get a higher probability;
  - "below / under / less than / at most / ..." a higher threshold must never
    get a lower one;
  - "by / before / until / no later than" a date: a later deadline must never
    get a lower probability;
  - anything else: the group is logged, no direction, no adjustment.
- The "consistent" shadow: isotonic regression (pool adjacent violators) of our
  submitted forecasts in that direction; unchanged when already consistent.
- Our latest submitted binary forecasts are kept in a small index in
  fall26-data (status/binary_forecasts.json), so siblings forecast in earlier
  runs count too.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from bot_helpers import PUBLIC_LOGGER_NAME

logger = logging.getLogger(PUBLIC_LOGGER_NAME)

INDEX_PATH = "status/binary_forecasts.json"

_MONTHS = {
    m: i + 1
    for i, names in enumerate(
        [("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",), ("june", "jun"),
         ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"), ("october", "oct"),
         ("november", "nov"), ("december", "dec")]
    )
    for m in names
}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
# A date: "October 15, 2026", "15 October 2026", "Oct 2026", "October", "2026-10-15", "Q4 2026", "end of 2026".
_DATE = re.compile(
    rf"\b(?:(?P<iso>\d{{4}}-\d{{2}}-\d{{2}})"
    rf"|(?P<q>Q[1-4])\s+(?P<qy>\d{{4}})"
    rf"|(?P<d1>\d{{1,2}})\s+(?P<m1>{_MONTH_RE})\.?,?(?:\s+(?P<y1>\d{{4}}))?"
    rf"|(?P<m2>{_MONTH_RE})\.?(?:\s+(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?)?,?(?:\s+(?P<y2>\d{{4}}))?)\b",
    re.IGNORECASE,
)
# A number: "100", "1,000", "2.5", "$1.2B", "10k", "3.5%".
_NUMBER = re.compile(r"(?<![\w.])\$?(?P<n>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?(?P<s>[kKmMbBtT](?:n|illion)?\b|%)?")
_SCALE = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}

_HIGHER_IS_HARDER = re.compile(
    r"\b(above|exceed(?:s|ing)?|more than|greater than|higher than|at least|over|surpass(?:es)?|reach(?:es)?|"
    r"or more|or higher|or above|top)\W*(?:\$|US\$)?\s*$", re.IGNORECASE)
_LOWER_IS_HARDER = re.compile(
    r"\b(below|under|less than|fewer than|lower than|at most|no more than|or less|or fewer|or lower|drop below|fall below)\W*(?:\$|US\$)?\s*$",
    re.IGNORECASE)
_DEADLINE = re.compile(r"\b(by|before|until|no later than|prior to|by the end of|by end of)\W*$", re.IGNORECASE)


@dataclass(frozen=True)
class Token:
    kind: str  # "n" (number) or "d" (date)
    value: tuple  # comparable
    start: int
    end: int


def _date_value(m: re.Match) -> tuple:
    g = m.groupdict()
    if g["iso"]:
        y, mo, d = (int(x) for x in g["iso"].split("-"))
        return (y, mo, d)
    if g["q"]:
        return (int(g["qy"]), int(g["q"][1]) * 3, 31)
    month = _MONTHS[(g["m1"] or g["m2"]).lower()]
    day = int(g["d1"] or g["d2"] or 31)
    year = int(g["y1"] or g["y2"] or 0)
    return (year, month, day)


def tokens(text: str) -> list[Token]:
    """Dates first, then the numbers outside them, in order of position."""
    found: list[Token] = []
    taken: list[tuple[int, int]] = []
    for m in _DATE.finditer(text):
        found.append(Token("d", _date_value(m), m.start(), m.end()))
        taken.append((m.start(), m.end()))
    for m in _NUMBER.finditer(text):
        if any(a <= m.start() < b for a, b in taken):
            continue
        value = float(m.group("n").replace(",", ""))
        suffix = (m.group("s") or "").lower()
        if suffix and suffix[0] in _SCALE:
            value *= _SCALE[suffix[0]]
        found.append(Token("n", (value,), m.start(), m.end()))
    return sorted(found, key=lambda t: t.start)


def skeleton(text: str) -> tuple[str, list[Token]]:
    """The title with each number/date replaced by a placeholder, and the tokens."""
    toks = tokens(text)
    out, last = [], 0
    for t in toks:
        out.append(text[last:t.start])
        out.append(f"<{t.kind}>")
        last = t.end
    out.append(text[last:])
    return re.sub(r"\s+", " ", "".join(out)).strip().lower(), toks


@dataclass
class Group:
    slot: int  # which number/date varies
    kind: str  # "n" or "d"
    direction: str  # "decreasing", "increasing" or "none" (probability as the value grows)
    members: list[tuple[Any, tuple]]  # (question key, value), sorted by value


def _direction(prefix_text: str, kind: str) -> str:
    if kind == "d":
        return "increasing" if _DEADLINE.search(prefix_text) else "none"
    if _HIGHER_IS_HARDER.search(prefix_text):
        return "decreasing"
    if _LOWER_IS_HARDER.search(prefix_text):
        return "increasing"
    return "none"


def find_groups(questions: dict[Any, str]) -> list[Group]:
    """questions: key -> title. Groups of 2+ siblings differing in exactly one
    number or date (all else identical)."""
    by_skeleton: dict[str, list[tuple[Any, str, list[Token]]]] = {}
    for key, text in questions.items():
        skel, toks = skeleton(text or "")
        if toks:
            by_skeleton.setdefault(skel, []).append((key, text, toks))
    groups = []
    for members in by_skeleton.values():
        if len(members) < 2:
            continue
        n = len(members[0][2])
        differing = [i for i in range(n) if len({m[2][i].value for m in members}) > 1]
        if len(differing) != 1:
            continue  # identical, or more than one thing changes: not a clean ladder
        slot = differing[0]
        values = [m[2][slot].value for m in members]
        if len(set(values)) != len(values):
            continue  # two questions with the same value: ambiguous
        key0, text0, toks0 = members[0]
        direction = _direction(text0[: toks0[slot].start], toks0[slot].kind)
        ordered = sorted(((m[0], m[2][slot].value) for m in members), key=lambda kv: kv[1])
        groups.append(Group(slot, toks0[slot].kind, direction, ordered))
    return groups


def isotonic(values: list[float], increasing: bool) -> list[float]:
    """Pool adjacent violators (equal weights)."""
    ys = values if increasing else [-v for v in values]
    blocks: list[list[float]] = []  # [sum, count]
    for y in ys:
        blocks.append([y, 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, c = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += c
    out: list[float] = []
    for s, c in blocks:
        out += [s / c] * c
    return out if increasing else [-v for v in out]


def violations(values: list[float], increasing: bool) -> int:
    pairs = zip(values, values[1:])
    return sum(1 for a, b in pairs if (b < a - 1e-9 if increasing else b > a + 1e-9))


def consistent_forecasts(group: Group, forecasts: dict[Any, float]) -> tuple[dict[Any, float], int]:
    """(adjusted forecast per member, number of violations). Unchanged when the
    group has no direction."""
    keys = [k for k, _ in group.members]
    values = [forecasts[k] for k in keys]
    if group.direction == "none":
        return dict(zip(keys, values)), 0
    increasing = group.direction == "increasing"
    return dict(zip(keys, isotonic(values, increasing))), violations(values, increasing)


class ForecastIndex:
    """Our latest submitted binary forecast per open question (fall26-data)."""

    def __init__(self, store: Any) -> None:
        self._store = store
        try:
            self.data: dict[str, dict] = dict(store.load() or {})
        except Exception as e:
            logger.warning(f"Binary forecast index could not be loaded ({type(e).__name__})")
            self.data = {}

    def put(self, post: Any, title: str, close_time: datetime | None, forecast: float, tournament: str) -> None:
        self.data[str(post)] = {
            "title": title, "forecast": forecast, "tournament": tournament,
            "close_time": close_time.isoformat() if close_time else None,
            "at": datetime.now(timezone.utc).isoformat(),
        }

    def open_titles(self, tournament: str, now: datetime | None = None) -> dict[str, str]:
        now = now or datetime.now(timezone.utc)
        out = {}
        for post, e in self.data.items():
            close = datetime.fromisoformat(e["close_time"]) if e.get("close_time") else None
            if close is not None and close.tzinfo is None:
                close = close.replace(tzinfo=timezone.utc)
            if e.get("tournament") == tournament and (close is None or close > now):
                out[post] = e["title"]
        return out

    def save(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        keep = {}
        for post, e in self.data.items():
            close = datetime.fromisoformat(e["close_time"]) if e.get("close_time") else None
            if close is not None and close.tzinfo is None:
                close = close.replace(tzinfo=timezone.utc)
            if close is None or close > now:
                keep[post] = e
        self.data = keep
        try:
            self._store.save(self.data)
        except Exception as e:
            logger.warning(f"Binary forecast index could not be saved ({type(e).__name__})")


def shadow_for(index: ForecastIndex, post: Any, tournament: str) -> dict | None:
    """After `post` was submitted (and put in the index): its sibling group,
    violations and the consistent shadow value, or None if it has no siblings."""
    titles = index.open_titles(tournament)
    for group in find_groups(titles):
        keys = [k for k, _ in group.members]
        if str(post) not in keys:
            continue
        forecasts = {k: float(index.data[k]["forecast"]) for k in keys}
        adjusted, bad = consistent_forecasts(group, forecasts)
        return {
            "group": keys,
            "kind": "threshold" if group.kind == "n" else "date",
            "direction": group.direction,
            "violations": bad,
            "consistent": adjusted[str(post)],
        }
    return None
