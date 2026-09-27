"""
Runtime helpers for the template bot: environment validation, startup/result
banners, and suppression of noisy upstream warnings.

Kept separate from main.py so that file can focus on the bot's forecasting
logic. main_with_no_framework.py keeps its own inline copies on purpose --
it's meant to be a single-file reference implementation.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import traceback
import warnings
from typing import Any, Sequence

# The repo and its Actions logs are public. Only this logger's messages are
# shown in full; the bot's own code logs question ids, status and cost only.
PUBLIC_LOGGER_NAME = "fall26"


# Placeholder values shipped in .env.template. If a real env var still equals
# one of these the user forgot to replace it; we'd rather fail loudly here than
# inside the SDK three layers down.
_PLACEHOLDER_ENV_VALUES = {
    "1234567890",
    "REPLACE_ME",
    "your-token-here",
    "your-api-key-here",
}


def _is_real_env(name: str) -> bool:
    val = os.getenv(name)
    return bool(val and val.strip() and val.strip() not in _PLACEHOLDER_ENV_VALUES)


def silence_noisy_dependencies() -> None:
    """
    Quiet warnings from transitive deps that fire on import and confuse new
    users. Must be called *before* importing forecasting_tools.
    """
    warnings.filterwarnings(
        "ignore", message=r".*does not support cost tracking.*"
    )
    logging.getLogger("forecasting_tools.ai_models.model_tracker").setLevel(
        logging.ERROR
    )
    # Streamlit installs its own logger hierarchy; suppress via its own API.
    try:
        from streamlit.logger import set_log_level

        set_log_level("error")
    except ImportError:
        pass
    # LiteLLM is verbose at INFO; its WARNING level is enough for us.
    litellm_logger = logging.getLogger("LiteLLM")
    litellm_logger.setLevel(logging.WARNING)
    litellm_logger.propagate = False


def check_environment(strict: bool = True) -> None:
    """
    Verify METACULUS_TOKEN is set; warn if no LLM key is configured. On
    failure with strict=True, exits the process with a non-zero status.
    """
    problems: list[str] = []

    if not _is_real_env("METACULUS_TOKEN"):
        problems.append(
            "METACULUS_TOKEN is missing or still a placeholder. "
            "Get one at https://www.metaculus.com/futureeval/participate/"
        )

    has_llm_key = any(
        _is_real_env(k)
        for k in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
    )
    if not has_llm_key:
        print(
            "⚠️  No LLM key set (OPENROUTER/OPENAI/ANTHROPIC). The bot will fall back\n"
            "    to the Metaculus LLM proxy. Free OpenRouter credits: "
            "https://forms.gle/aQdYMq9Pisrf1v7d8\n"
        )

    if problems:
        print("❌  Setup problems:")
        for p in problems:
            print(f"    • {p}")
        if strict:
            sys.exit(1)


def print_startup_banner(run_mode: str, will_publish: bool) -> None:
    publish = "publish=yes" if will_publish else "publish=no (dry run)"
    print(f"🤖  Running mode={run_mode}, {publish}\n")


def print_run_summary_banner(
    forecast_reports: Sequence[Any],
    will_publish: bool,
    tournament_url: str | None = None,
) -> None:
    """
    End-of-run summary printed via print() (not logger) so it survives log
    filtering. Shows count, per-question URLs, and any failure tracebacks.
    If tournament_url is given, it's included as a footer link.
    """
    # Lazy import so this module is usable in contexts where forecasting_tools
    # isn't installed (e.g. unit tests of the banner format).
    from forecasting_tools import ForecastReport

    valid = [r for r in forecast_reports if isinstance(r, ForecastReport)]
    exceptions = [r for r in forecast_reports if isinstance(r, BaseException)]
    banner = "=" * 80

    print()
    print(banner)

    if not forecast_reports:
        print("ℹ️   No new questions to forecast on this run.")
        print(banner)
        print()
        return

    if valid and not exceptions:
        verb = "submitted" if will_publish else "produced (dry run)"
        print(f"🎉  Bot {verb} {len(valid)} forecast(s).")
    elif valid and exceptions:
        print(
            f"⚠️   Partial — {len(valid)} succeeded, {len(exceptions)} failed."
        )
    else:
        print(f"❌  All {len(exceptions)} attempt(s) failed.")

    if valid:
        print()
        for r in valid:
            note = f"  (with {len(r.errors)} minor error(s))" if r.errors else ""
            print(f"  ✅ {r.question.page_url}{note}")
        if will_publish and tournament_url:
            print(f"\n  Tournament: {tournament_url}")

    if exceptions:
        print()
        for exc in exceptions:
            print(f"  ❌ question {question_id_from_exception(exc)}: {describe_exception(exc)}")

    print(banner)
    print()


_POST_URL_PATTERN = re.compile(r"metaculus\.com/questions/(\d+)")


def write_cost_summary(
    forecast_reports: Sequence[Any], lineup_name: str, billed: bool = True
) -> None:
    """
    Cost per question as a markdown table in the GitHub Actions run summary
    ($GITHUB_STEP_SUMMARY), or printed when run elsewhere. Shows only the
    question id, type, status and cost, never forecasts or reasoning, since
    the repo and its run pages are public.
    """
    from forecasting_tools import ForecastReport

    lines = [
        f"## Cost per question (lineup: {lineup_name})",
        "",
        "| Question | Type | Status | Cost (USD) |",
        "|---|---|---|---|",
    ]
    total_cost = 0.0
    for report in forecast_reports:
        if isinstance(report, ForecastReport):
            question = report.question
            cost = report.price_estimate or 0.0
            total_cost += cost
            status = "ok" if not report.errors else f"ok, {len(report.errors)} minor error(s)"
            lines.append(
                f"| [{question.id_of_post}]({question.page_url}) "
                f"| {question.question_type} | {status} | {cost:.4f} |"
            )
        else:
            lines.append(
                f"| {question_id_from_exception(report)} | ? "
                f"| failed: {type(report).__name__} | ? |"
            )
    if not forecast_reports:
        lines.append("| - | - | no new questions | 0 |")
    if billed:
        lines += ["", f"**Total cost: ${total_cost:.4f}**", ""]
    else:
        # LiteLLM prices free-tier models at list price; nothing is billed.
        lines += [
            "",
            f"**Billed: $0 (free tier).** List-price equivalent: ${total_cost:.4f}",
            "",
        ]
    summary = "\n".join(lines)

    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(summary + "\n")
    print(summary)


def describe_exception(exc: BaseException) -> str:
    """
    Error types and where they were raised, without the error messages:
    library error messages can quote the model's reasoning or forecast.
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(parts) < 6:
        seen.add(id(current))
        part = type(current).__name__
        if current.__traceback__ is not None:
            frame = traceback.extract_tb(current.__traceback__)[-1]
            part += f" at {os.path.basename(frame.filename)}:{frame.lineno}"
        if isinstance(current, BaseExceptionGroup):
            part += " [" + "; ".join(describe_exception(e) for e in current.exceptions[:3]) + "]"
        if not parts or parts[-1] != part:
            parts.append(part)
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


def question_id_from_exception(exc: BaseException) -> str:
    match = _POST_URL_PATTERN.search(str(exc))
    return match.group(1) if match else "?"


class _PublicLogFilter(logging.Filter):
    """Hide the text of every log record not written by our own code."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == PUBLIC_LOGGER_NAME or record.name.startswith(
            PUBLIC_LOGGER_NAME + "."
        ):
            return True
        if record.levelno < logging.WARNING:
            return False
        detail = ""
        if record.exc_info and record.exc_info[1] is not None:
            detail = f" ({describe_exception(record.exc_info[1])})"
        record.msg = f"[message hidden: public repo]{detail}"
        record.args = None
        record.exc_info = None
        record.exc_text = None
        return True


def configure_public_logging() -> None:
    """
    Logging for a public repo: our own INFO messages (ids, status, cost), and
    only the level and logger name of warnings/errors from libraries. Python
    warnings and uncaught errors go through the same filter.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    handler.addFilter(_PublicLogFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger(PUBLIC_LOGGER_NAME).setLevel(logging.INFO)
    # LiteLLM has its own handlers; route it through ours instead.
    for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
        litellm_logger = logging.getLogger(name)
        litellm_logger.handlers = []
        litellm_logger.propagate = True
        litellm_logger.setLevel(logging.WARNING)
    # e.g. pydantic serializer warnings, which can quote model output.
    logging.captureWarnings(True)

    def _excepthook(exc_type, exc, tb):  # type: ignore[no-untyped-def]
        print(f"Unhandled error: {describe_exception(exc)}", file=sys.stderr)
        print("".join(traceback.format_tb(tb)), file=sys.stderr)

    sys.excepthook = _excepthook


def log_question_statuses(forecast_reports: Sequence[Any]) -> int:
    """Log one line per question (id, status, cost). Returns the number of failures."""
    from forecasting_tools import ForecastReport

    logger = logging.getLogger(PUBLIC_LOGGER_NAME)
    failures = 0
    for report in forecast_reports:
        if isinstance(report, ForecastReport):
            minor = f", {len(report.errors)} minor error(s)" if report.errors else ""
            logger.info(
                f"Question {report.question.id_of_post}: submitted{minor}, "
                f"cost ${report.price_estimate or 0:.4f}"
            )
        else:
            failures += 1
            logger.error(
                f"Question {question_id_from_exception(report)}: FAILED, "
                f"{describe_exception(report)}"
            )
    return failures
