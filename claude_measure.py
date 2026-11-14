"""
Claude measurement on the expiring credit (architect, 14 Nov). Test-only, never
submits, runs in the Replay lab's own queue (never the live one).

On ALL resolved tournament questions with a frozen dossier (question log
"dossier_sections"), plain Fable 5.2 and plain Opus 5.5 (effort high, NO
tools, no web) forecast from the bot's own prompt + the frozen dossier. Each is
scored against the outcome, next to:
- what we submitted (the latest submitted log's final forecast),
- Flash (the live combine of that log's Flash forecasts),
- claude_first (the Claude-only combine: the live Claude forecasts when there
  were 2+, else the live ones plus these two replays).

Money (fail closed; the live bot keeps priority):
- lab cap LAB_CAP ($14), charged at the real token cost;
- before every call: lab spend + the call's reservation (the live worst case,
  Fable $0.72, Opus $0.36) must stay under the cap, and the cycle (live ledger
  + lab) must keep at least CYCLE_FLOOR ($9) left after it;
- no call starts at or after OLD_CREDIT_END (the old credit expires);
- a credit error stops the run at once (never retried).
The live ledger (status/claude_spend.json) is only READ: the lab keeps its
own spend in lab/claude_measure.json (a live save would fail on a stale file).

Results (private, fall26-data): lab/claude_measure.json (per question:
type, replay forecasts and costs) and lab/claude_measure.md (the per-model
table). The public log shows counts and dollars only.
"""
from __future__ import annotations

import asyncio
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

LAB_CAP = 14.00
CYCLE_FLOOR = 9.00
OLD_CREDIT_END = datetime(2026, 11, 16, tzinfo=timezone.utc)  # Tony, 14 Nov: the credits renew on the 16th
RESULTS_PATH = "lab/claude_measure.json"
REPORT_PATH = "lab/claude_measure.md"
WORKERS = 4
SAVE_EVERY = 8  # questions
RUN_LIMIT_SECONDS = 100 * 60  # the job's timeout is 120 minutes
CALL_SECONDS = 6 * 60  # one Claude call's worst case (its timeout)
GROUPS = (("binary", ("binary",)), ("numeric + discrete", ("numeric", "discrete")), ("multiple choice", ("multiple_choice",)))
COLUMNS = ("submitted", "flash", "claude_first", "opus", "fable")


class CreditError(Exception):
    """'credit balance is too low': the run stops at once."""


# ---------------------------------------------------------------- money


@dataclass
class LabMoney:
    """The lab's spend and its two limits. Thread-safe: a call reserves its worst
    case first and is charged its real cost after."""

    spent: float
    live_cycle_spent: Callable[[], float]  # the live ledger's cycle spend, read fresh
    budget: float
    cap: float = LAB_CAP
    floor: float = CYCLE_FLOOR
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    stopped: str | None = None
    in_flight: float = 0.0
    calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def reserve(self, amount: float) -> bool:
        with self._lock:
            if self.stopped:
                return False
            if self.now() >= OLD_CREDIT_END:
                self.stopped = "the old credit has expired"
                return False
            committed = self.spent + self.in_flight + amount
            if committed > self.cap + 1e-9:
                self.stopped = f"lab cap ${self.cap:.2f} reached"
                return False
            try:
                live = float(self.live_cycle_spent())
            except Exception:
                self.stopped = "live ledger could not be read"
                return False
            if self.budget - live - committed < self.floor - 1e-9:
                self.stopped = f"the cycle would have under ${self.floor:.2f} left"
                return False
            self.in_flight += amount
            self.calls += 1
            return True

    def charge(self, reserved: float, cost: float) -> None:
        with self._lock:
            self.in_flight = max(0.0, self.in_flight - reserved)
            self.spent += cost

    def stop(self, why: str) -> None:
        with self._lock:
            self.stopped = self.stopped or why


# ---------------------------------------------------------------- one replay forecast


def claude_forecaster(model: str, costs: list[float]) -> Any:
    """A plain Claude forecaster (frontier._ClaudeCall: effort high, no tools,
    one try) that appends each answered call's real cost to `costs`."""
    import frontier
    from llm_throttle import NoAnswer

    class Measured(frontier._ClaudeCall):
        async def _mockable_direct_call_to_model(self, prompt: Any):  # type: ignore[no-untyped-def]
            try:
                reply = await super()._mockable_direct_call_to_model(prompt)
            except NoAnswer as e:
                costs.append(float(getattr(e, "cost", 0.0) or 0.0))
                raise
            except Exception as e:
                if frontier.is_credit_error(e):
                    raise CreditError("credit balance is too low") from None
                costs.append(frontier.claude_reserve(model))  # may have been billed: charge the worst case
                raise
            costs.append(float(reply.cost or 0.0))
            return reply

    return Measured(model=model, temperature=None, timeout=frontier.CLAUDE_TIMEOUT_SECONDS, allowed_tries=1)


def bot_forecast(question_obj: Any, research: str, llm: Any) -> Any:
    """The bot's own prompt for the type, read directly (no parser call)."""
    from forecasting_tools import BinaryQuestion, MultipleChoiceQuestion

    import main
    from probe import NoParser

    parser = NoParser()
    bot = main.FallBot2026(
        llms={"default": llm, "parser": parser, "summarizer": parser, "researcher": "no_research"},
        publish_reports_to_metaculus=False, enable_summarize_research=False,
    )
    bot._structure_output_validation_samples = 1
    if isinstance(question_obj, BinaryQuestion):
        run = bot._run_forecast_on_binary(question_obj, research)
    elif isinstance(question_obj, MultipleChoiceQuestion):
        run = bot._run_forecast_on_multiple_choice(question_obj, research)
    else:
        run = bot._run_forecast_on_numeric(question_obj, research)
    return asyncio.run(run).prediction_value


def replay(model: str, question_obj: Any, research: str, money: LabMoney,
           forecast: Callable[[str, Any, str, list[float]], Any] | None = None) -> tuple[Any, float, str]:
    """(forecast in the saved JSON form or None, cost, status) of one call."""
    import frontier
    from question_log import to_jsonable

    reserved = frontier.claude_reserve(model)
    if not money.reserve(reserved):
        return None, 0.0, "not run: " + (money.stopped or "?")
    costs: list[float] = []
    status = "ok"
    value = None
    try:
        if forecast is None:
            value = bot_forecast(question_obj, research, claude_forecaster(model, costs))
        else:
            value = forecast(model, question_obj, research, costs)
        value = to_jsonable(value)
    except CreditError:
        money.stop("credit error")
        status = "credit error"
    except Exception as e:
        status = f"failed ({type(e).__name__})"
    cost = sum(costs)
    money.charge(reserved, cost)
    return value, cost, status


# ---------------------------------------------------------------- scoring one question


def flash_values(record: dict) -> list[Any]:
    from aggregation import record_models_and_values

    return [v for m, v in record_models_and_values(record) if m and "flash" in m.lower()]


def question_row(record: dict, resolution: str, replays: dict[str, Any],
                 combine: Callable[[dict, list], Any] | None = None) -> dict | None:
    """Scores of one resolved question: submitted, flash, claude_first, opus, fable (None = not available)."""
    from aggregation import combine_saved, is_claude_forecast, record_models_and_values
    from scoreboard import forecast_score, live_score

    combine = combine or combine_saved
    snapshot = record.get("question") or {}
    kind = snapshot.get("question_type")
    submitted = live_score(record, resolution)
    if submitted is None:
        return None

    def score(value: Any) -> float | None:
        try:
            return forecast_score(kind, value, resolution)
        except Exception:
            return None

    def combined(values: list[Any]) -> float | None:
        if not values:
            return None
        try:
            return score(combine(snapshot, values))
        except Exception:
            return None

    live_claude = [v for m, v in record_models_and_values(record) if is_claude_forecast(m)]
    claude_values = live_claude if len(live_claude) >= 2 else live_claude + [v for v in replays.values() if v is not None]
    return {
        "post": snapshot.get("id_of_post"), "type": kind,
        "submitted": submitted,
        "flash": combined(flash_values(record)),
        "claude_first": combined(claude_values) if len(claude_values) >= 2 else None,
        "claude_first_from": "live" if len(live_claude) >= 2 else f"{len(live_claude)} live + replays",
        "opus": score(replays.get("opus")),
        "fable": score(replays.get("fable")),
    }


# ---------------------------------------------------------------- the table


def table(rows: list[dict]) -> str:
    """Per type group: questions and the mean score of each forecaster (higher is
    better; log scores, so only compare within a group), plus paired means."""
    def mean(values: list[float]) -> str:
        return f"{statistics.fmean(values):+.3f}" if values else "n/a"

    lines = ["| Type | Questions | Submitted | Flash | claude_first | Plain Opus 5.5 (replay) | Plain Fable 5.2 (replay) |",
             "|---|---|---|---|---|---|---|"]
    paired = ["", "**Paired** (only questions where both have a score; difference = column minus submitted, + = better):", "",
              "| Type | Flash | claude_first | Plain Opus | Plain Fable | Fable minus Opus |", "|---|---|---|---|---|---|"]
    for name, kinds in (*GROUPS, ("all", tuple(k for _, ks in GROUPS for k in ks))):
        group = [r for r in rows if r["type"] in kinds]
        cells = []
        for column in COLUMNS:
            values = [r[column] for r in group if r[column] is not None]
            cells.append(f"{mean(values)} (n={len(values)})")
        if name != "all":
            lines.append(f"| {name} | {len(group)} | " + " | ".join(cells) + " |")
        diffs = []
        for column in ("flash", "claude_first", "opus", "fable"):
            d = [r[column] - r["submitted"] for r in group if r[column] is not None]
            diffs.append(f"{mean(d)} (n={len(d)})")
        fo = [r["fable"] - r["opus"] for r in group if r["fable"] is not None and r["opus"] is not None]
        diffs.append(f"{mean(fo)} (n={len(fo)})")
        paired.append(f"| {name} | " + " | ".join(diffs) + " |")
    return "\n".join(lines + paired)


# ---------------------------------------------------------------- the run


@dataclass
class Source:
    """The outside world (injectable for tests)."""

    paths: list[str]
    load: Callable[[str], dict]
    resolution_of: Callable[[Any], str | None]


def resolved_records(source: Source, may_continue: Callable[[], bool]) -> Iterable[tuple[dict, str]]:
    """(latest submitted log with a frozen dossier, resolution), newest post first."""
    by_post: dict[int, list[str]] = {}
    for path in source.paths:
        name = path.rsplit("/", 1)[-1]
        if not path.startswith("questions/tournament/") or not name.endswith(".json") or name.endswith("_shadows.json"):
            continue
        try:
            by_post.setdefault(int(name.split("_", 1)[0]), []).append(path)
        except ValueError:
            continue
    for post in sorted(by_post, reverse=True):
        if not may_continue():
            return
        try:
            resolution = source.resolution_of(post)
        except Exception:
            continue
        if not resolution:
            continue
        for path in sorted(by_post[post], key=lambda p: p.split("/")[-2:], reverse=True):  # newest log first
            try:
                record = source.load(path)
            except Exception:
                continue
            if record.get("submitted"):
                if (record.get("dossier_sections") or {}).get("base", {}).get("text"):
                    yield record, resolution
                break


MODELS = {"fable": "anthropic/claude-fable-5-2", "opus": "anthropic/claude-opus-5-5"}


def run(source: Source, money: LabMoney, saved: dict, save: Callable[[dict], None],
        forecast: Callable[[str, Any, str, list[float]], Any] | None = None,
        clock: Callable[[], float] = time.monotonic, limit: float = RUN_LIMIT_SECONDS,
        workers: int = WORKERS) -> tuple[list[dict], dict]:
    """Replays every resolved question (resuming from `saved`), then scores all."""
    import lab

    started = clock()
    questions: dict[str, dict] = dict(saved.get("questions") or {})
    lock = threading.Lock()
    done_since_save = [0]

    def may_continue() -> bool:
        return money.stopped is None and clock() - started + CALL_SECONDS <= limit

    def one(record: dict) -> None:
        snapshot = record["question"]
        post = str(snapshot.get("id_of_post"))
        entry = questions.get(post) or {"type": snapshot.get("question_type")}
        try:
            question_obj = lab._question_object(snapshot)
            research = lab.frozen_dossier(record.get("dossier_sections") or {})
        except Exception:
            return
        if research is None:
            return
        ran = False
        for key, model in MODELS.items():
            if entry.get(key, {}).get("status") == "ok" or not may_continue():
                continue
            value, cost, status = replay(model, question_obj, research, money, forecast)
            if status.startswith("not run"):
                continue
            entry[key] = {"forecast": value, "cost": round(cost, 6), "status": status}
            ran = True
        if not ran:
            return
        with lock:
            questions[post] = entry
            done_since_save[0] += 1
            if done_since_save[0] >= SAVE_EVERY:
                done_since_save[0] = 0
                save({"questions": questions, "spent": round(money.spent, 6)})

    records: list[tuple[dict, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []
        for record, resolution in resolved_records(source, may_continue):
            records.append((record, resolution))
            post = str(record["question"].get("id_of_post"))
            have = questions.get(post) or {}
            if all(have.get(k, {}).get("status") == "ok" for k in MODELS):
                continue
            futures.append(pool.submit(one, record))
        for f in futures:
            f.result()
    data = {"questions": questions, "spent": round(money.spent, 6)}
    save(data)
    rows = []
    for record, resolution in records:
        post = str(record["question"].get("id_of_post"))
        entry = questions.get(post) or {}
        replays = {k: (entry.get(k) or {}).get("forecast") for k in MODELS}
        row = question_row(record, resolution, replays)
        if row is not None:
            rows.append(row)
    return rows, data


def main_run() -> str:
    """The real run (workflow 'Replay lab', claude_measure): real Claude calls."""
    import json
    import os

    import requests

    import bot_config
    import frontier
    import scoreboard
    from bot_helpers import silence_noisy_dependencies
    from gemini_budget import GitHubFileStore
    from question_log import DATA_REPO

    silence_noisy_dependencies()
    from forecasting_tools import MetaculusClient

    token = os.environ["DATA_REPO_TOKEN"]
    if not os.getenv("ANTHROPIC_API_KEY"):
        return "Claude measurement: ANTHROPIC_API_KEY not set, nothing run."
    results_store = GitHubFileStore(DATA_REPO, RESULTS_PATH, token)
    saved = results_store.load() or {}
    live_store = GitHubFileStore(DATA_REPO, frontier.CLAUDE_SPEND_PATH, token)
    reset = frontier.reset_date(bot_config.CLAUDE_CYCLE_RESET_DATE)

    def live_cycle_spent() -> float:
        ledger = live_store.load()
        if ledger is None:
            raise RuntimeError("no live ledger")
        cycle = frontier.cycle_start(datetime.now(timezone.utc).date(), reset)
        return float((ledger.get("cycles") or {}).get(cycle.isoformat(), 0.0)) if cycle else float("inf")

    money = LabMoney(float(saved.get("spent", 0.0)), live_cycle_spent, bot_config.CLAUDE_BUDGET_PER_CYCLE)
    client = MetaculusClient()

    def resolution_of(post: Any) -> str | None:
        try:
            question = client.get_question_by_post_id(post)
        except Exception:
            time.sleep(3)  # Metaculus rate limit (no retries in the client): one more try
            question = client.get_question_by_post_id(post)
        if isinstance(question, list):
            question = question[0]
        return getattr(question, "resolution_string", None)

    def load(path: str) -> dict:
        response = requests.get(f"https://api.github.com/repos/{DATA_REPO}/contents/{path}", timeout=60,
                                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw"})
        response.raise_for_status()
        return json.loads(response.text)

    paths = [t["path"] for t in scoreboard._github("git/trees/HEAD?recursive=1", token)["tree"]]

    def save(data: dict) -> None:
        try:
            results_store.save(data, message="Claude measurement")
        except Exception:
            print("::warning::Claude measurement results could not be saved")

    rows, data = run(Source(paths, load, resolution_of), money, saved, save)
    entries = data["questions"].values()
    counts = {k: sum(1 for e in entries if (e.get(k) or {}).get("status") == "ok") for k in MODELS}
    note = (f"Resolved questions scored: {len(rows)}; replays answered: Fable {counts['fable']}, Opus {counts['opus']}; "
            f"lab spend ${money.spent:.2f} of ${LAB_CAP:.2f} ({money.calls} calls this run); "
            f"stopped: {money.stopped or 'no, all done'}; nothing submitted.")
    report = f"# Claude measurement (architect, 14 Nov)\n\n{note}\n\n" + table(rows) + "\n\n" + "\n".join(
        f"- {r['post']} {r['type']}: claude_first from {r['claude_first_from']}" for r in rows)
    try:
        _save_text(DATA_REPO, REPORT_PATH, token, report)
    except Exception:
        print("::warning::Claude measurement report could not be saved")
    return "Claude measurement: " + note + f" The table is in fall26-data {REPORT_PATH} (private)."


def _save_text(repo: str, path: str, token: str, text: str) -> None:
    import base64

    import requests

    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    current = requests.get(url, headers=headers, timeout=30)
    body = {"message": "Claude measurement report", "content": base64.b64encode(text.encode()).decode()}
    if current.status_code == 200:
        body["sha"] = current.json()["sha"]
    requests.put(url, headers=headers, json=body, timeout=30).raise_for_status()


if __name__ == "__main__":
    import os

    text = main_run()
    print(text)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    # 14 Nov: the first run saved everything at 20:11 UTC, then never exited (a
    # library thread left running). Everything is saved by now: leave at once.
    import sys

    sys.stdout.flush()
    os._exit(0)
