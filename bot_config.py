"""
All model choices for FallBot2026, in one place.

Three lineups:
- "free": OpenRouter ':free' models, for the bot-testing-area only
  (OPENROUTER_API_KEY is a $0 key until Metaculus sends the credit key).
- "gemini-free" (active now, PLAN.md "Plan B"): Gemini 3.6 Flash on the free
  Google AI Studio key (GEMINI_API_KEY, no billing), paced to stay inside the
  free rate limits. Allowed on real questions once BOT_ENABLED is true.
- "credits" (prepared, OFF until the architect says so): the full PLAN.md
  section 3 lineup for the Metaculus credit key, with spending tiers
  (ensemble.py, CreditsPlanner).

To switch lineups, change ACTIVE_LINEUP.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from forecasting_tools import GeneralLlm

import ensemble
from gemini_budget import QuotaLedger, make_store
from llm_throttle import RequestPacer, ThrottledLlm

ACTIVE_LINEUP: Literal["free", "gemini-free", "credits"] = "gemini-free"

# PLAN.md Step 7 (research.py): planner -> AskNews (at most 3 calls per
# question, ASKNEWS_API_KEY) or free news -> dossier -> gap-fill. The planner
# and dossier writer are the lineup's parser model.
RESEARCHER = "planned-research"

# How OpenRouter is asked for high reasoning effort (see NOTES.md).
HIGH_REASONING = {"reasoning": {"effort": "high"}}

# Free test model. Picked because it is a large free model on OpenRouter
# that supports structured output.
FREE_MODEL = "openrouter/nvidia/nemotron-3-super-120b-a12b:free"

# Google AI Studio directly (LiteLLM "gemini/" prefix, reads GEMINI_API_KEY).
# Free tier (AI Studio rate-limit page, 27 Sep 2026): 5 requests/minute and
# 20 requests/day, per model, per project. The day resets at midnight Pacific.
# Forecasting pool: each question gets forecasts from different versions.
GEMINI_FORECAST_MODELS = (
    "gemini/gemini-3.6-flash",
    "gemini/gemini-3.7-flash",
    "gemini/gemini-3.8-flash",
    "gemini/gemini-3.5-flash",
)
# Parser only, never a forecaster. The architect asked for Gemini 2.5 Flash /
# Flash-Lite, but Google closed both to new projects ("no longer available to
# new users", 27 Sep 2026), so the lighter 3.x Flash-Lite models parse instead.
# Step 7 also runs the research planner and dossier writer on these (2 calls
# per question), so a third model adds capacity; if it shares a quota with
# another, Google's 429 simply marks it used up.
GEMINI_PARSER_MODELS = (
    "gemini/gemini-3.5-flash-lite",
    "gemini/gemini-3.1-flash-lite",
    "gemini/gemini-3.1-flash-lite-preview",
)
GEMINI_FREE_REQUESTS_PER_DAY = 20
# Each model's calls are paced under the 5/minute limit.
GEMINI_FREE_REQUESTS_PER_MINUTE = 4
# 20% of each model's day is held back for questions that would otherwise
# get no forecast at all.
GEMINI_FREE_RESERVE = 0.2
GEMINI_MAX_FORECASTS_PER_QUESTION = 3
# Binary round 2 (Step 6): at most this many extra forecasts.
GEMINI_ROUND2_MAX = 2
# Budget counts as "low" below this share of the pool's usable daily total;
# then MiniBench questions get 1 forecast (seasonal ones still up to 3).
GEMINI_LOW_BUDGET_FRACTION = 0.25
# The day's counts live in the private fall26-data repo.
GEMINI_LEDGER_PATH = "quota/gemini_free.json"



def is_free_model(model: str) -> bool:
    """
    True for models that cannot be charged: OpenRouter ':free' models, and
    Gemini through the Google AI Studio key (free tier, no billing on it).
    Every other model, including every paid OpenRouter model, is False.
    """
    if model.startswith("openrouter/"):
        return model.endswith(":free")
    return model.startswith("gemini/")


@dataclass(frozen=True)
class Lineup:
    name: str
    llms: dict[str, str | GeneralLlm]
    research_reports_per_question: int
    predictions_per_research_report: int
    # structure_output parses each forecast this many times and checks the
    # answers agree. Each sample is one extra parser call.
    parser_validation_samples: int
    # The research summary only goes into the comment posted with the forecast.
    summarize_research: bool
    # Every LLM must pass is_free_model.
    free_only: bool
    # Only allowed in test_questions mode (the bot-testing-area).
    test_only: bool
    # Plans each question's forecasters (rounds, budget): GeminiPool for
    # gemini-free, CreditsPlanner for credits. None = the library's default.
    planner: GeminiPool | CreditsPlanner | None = None

    def llm_model_names(self) -> list[str]:
        """Model names of the LLMs and their backups (AskNews is not an LLM)."""
        names = []
        for llm in self.llms.values():
            while isinstance(llm, GeneralLlm):
                names.append(llm.model)
                llm = getattr(llm, "_backup", None)
        return names


def _free_lineup() -> Lineup:
    # Free models have tight rate limits, so keep calls per question low:
    # 1 forecast + 1 parse, no research summary.
    free_llm = GeneralLlm(
        model=FREE_MODEL,
        temperature=0.3,
        timeout=180,
        allowed_tries=3,
    )
    return Lineup(
        name="free",
        llms={
            "default": free_llm,
            "parser": free_llm,
            "summarizer": free_llm,
            "researcher": RESEARCHER,
        },
        research_reports_per_question=1,
        predictions_per_research_report=1,
        parser_validation_samples=1,
        summarize_research=False,
        free_only=True,
        test_only=True,
    )


@dataclass
class GeminiPool:
    """
    Plans which free Gemini models forecast each question, within the day's
    budget (see gemini_budget.QuotaLedger).
    """

    ledger: QuotaLedger
    pacers: dict[str, RequestPacer]

    def _forecaster(self, model: str, **kwargs) -> ThrottledLlm:
        return ThrottledLlm(
            model=model,
            pacer=self.pacers[model],
            ledger=self.ledger,
            temperature=None,
            timeout=300,
            # No retries on the same model: a failed call moves on to the next
            # model, so retries don't eat the day's quota.
            allowed_tries=1,
            # LiteLLM turns this into Gemini's thinkingLevel "high". (Setting
            # thinkingConfig through extra_body is overwritten with "low".)
            reasoning_effort="high",
            **kwargs,
        )

    def _chain(self, models: list[str], allow_reserve: bool) -> ThrottledLlm:
        """models[0] (booked) with the rest behind it as backups."""
        llm: ThrottledLlm | None = None
        for position, model in reversed(list(enumerate(models))):
            llm = self._forecaster(
                model, backup=llm, booked=position == 0, allow_reserve=allow_reserve
            )
        assert llm is not None
        return llm

    def is_budget_low(self) -> bool:
        usable_left = sum(
            max(0, self.ledger.usable_left(m)) for m in GEMINI_FORECAST_MODELS
        )
        usable_total = len(GEMINI_FORECAST_MODELS) * GEMINI_FREE_REQUESTS_PER_DAY * (
            1 - GEMINI_FREE_RESERVE
        )
        return usable_left < GEMINI_LOW_BUDGET_FRACTION * usable_total

    def save(self) -> None:
        self.ledger.save()

    def round2(self, seasonal: bool, used_models: list[str]) -> list[ThrottledLlm]:
        """
        Binary round 2 (only called when round 1 disagrees or is extreme): up
        to 2 more forecasts from models not used in round 1, from usable
        budget only (never the reserve).
        """
        spare = [
            m
            for m in sorted(
                GEMINI_FORECAST_MODELS,
                key=lambda m: (-self.ledger.usable_left(m), GEMINI_FORECAST_MODELS.index(m)),
            )
            if m not in used_models and self.ledger.usable_left(m) > 0
        ][:GEMINI_ROUND2_MAX]
        for model in spare:
            self.ledger.book(model)
        return [self._chain([model], allow_reserve=False) for model in spare]

    def plan(self, seasonal: bool, only_model: str | None = None, binary: bool = True) -> list[ThrottledLlm]:
        """
        One forecaster chain per forecast for a question: up to 3 different
        models with usable budget (MiniBench: 1 when the budget is low). If no
        model has usable budget, 1 forecast from the reserve. Empty if the
        day's quota is gone.
        """
        models = (only_model,) if only_model else GEMINI_FORECAST_MODELS
        ranked = sorted(
            models,
            key=lambda m: (-self.ledger.usable_left(m), GEMINI_FORECAST_MODELS.index(m)),
        )
        most = (
            GEMINI_MAX_FORECASTS_PER_QUESTION
            if seasonal or not self.is_budget_low()
            else 1
        )
        chosen = [m for m in ranked if self.ledger.usable_left(m) > 0][:most]
        if not chosen:
            chosen = [m for m in ranked if self.ledger.total_left(m) > 0][:1]
        for model in chosen:
            self.ledger.book(model)
        spare = [m for m in ranked if m not in chosen]
        return [
            # The first forecast may use the reserve, so every question gets one.
            self._chain([model, *spare], allow_reserve=index == 0)
            for index, model in enumerate(chosen)
        ]

    def any_quota_left(self) -> bool:
        return any(self.ledger.total_left(m) > 0 for m in GEMINI_FORECAST_MODELS)

    def quick_forecaster(self) -> ThrottledLlm:
        """
        For the one quick forecast when no planned forecast finished: every
        forecasting model, most quota left first, reserve allowed.
        """
        ranked = sorted(
            GEMINI_FORECAST_MODELS,
            key=lambda m: (-self.ledger.total_left(m), GEMINI_FORECAST_MODELS.index(m)),
        )
        llm: ThrottledLlm | None = None
        for model in reversed(ranked):
            llm = self._forecaster(model, backup=llm, allow_reserve=True)
        assert llm is not None
        return llm

    def unplanned_forecaster(self) -> ThrottledLlm:
        llm: ThrottledLlm | None = None
        for model in reversed(GEMINI_FORECAST_MODELS):
            llm = self._forecaster(model, backup=llm)
        assert llm is not None
        return llm

    def parser(self) -> ThrottledLlm:
        llm: ThrottledLlm | None = None
        for model in reversed(GEMINI_PARSER_MODELS):
            llm = ThrottledLlm(
                model=model,
                pacer=self.pacers[model],
                ledger=self.ledger,
                backup=llm,
                allow_reserve=True,
                temperature=None,
                timeout=180,
                # structure_output already retries a bad parse up to 3 times.
                allowed_tries=1,
            )
        assert llm is not None
        return llm


def _gemini_free_lineup() -> Lineup:
    all_models = (*GEMINI_FORECAST_MODELS, *GEMINI_PARSER_MODELS)
    pool = GeminiPool(
        ledger=QuotaLedger(
            make_store(GEMINI_LEDGER_PATH),
            daily_limits={m: GEMINI_FREE_REQUESTS_PER_DAY for m in all_models},
            reserve_fraction=GEMINI_FREE_RESERVE,
        ),
        pacers={m: RequestPacer(GEMINI_FREE_REQUESTS_PER_MINUTE) for m in all_models},
    )
    parser = pool.parser()
    return Lineup(
        name="gemini-free",
        llms={
            # Forecasts normally use the chains from pool.plan(); this is
            # only a fallback: the whole pool, never the reserve.
            "default": pool.unplanned_forecaster(),
            "parser": parser,
            "summarizer": parser,
            "researcher": RESEARCHER,
        },
        research_reports_per_question=1,
        predictions_per_research_report=GEMINI_MAX_FORECASTS_PER_QUESTION,
        parser_validation_samples=1,
        summarize_research=False,
        free_only=True,
        test_only=False,
        planner=pool,
    )


@dataclass
class CreditsPlanner:
    """
    The PLAN.md section 3 lineup on the Metaculus credit key (OFF until the
    architect says so). The spending tier (ensemble.py) is chosen once per
    run; MiniBench runs one tier below. Every forecaster: high reasoning,
    600 s timeout, 3 tries, then its backups (ensemble.BACKUPS).
    """

    seasonal_tier: str = "standard"
    pacer: RequestPacer = field(default_factory=lambda: RequestPacer(600))

    def _tier(self, seasonal: bool) -> ensemble.Tier:
        name = self.seasonal_tier if seasonal else ensemble.tier_below(self.seasonal_tier)
        return ensemble.TIERS[name]

    def _chain(self, model: str) -> ThrottledLlm:
        llm: ThrottledLlm | None = None
        for name in reversed([model, *ensemble.BACKUPS.get(model, [])]):
            llm = ThrottledLlm(
                model=name,
                pacer=self.pacer,
                backup=llm,
                temperature=None,
                timeout=600,
                allowed_tries=3,
                extra_body=HIGH_REASONING,
            )
        assert llm is not None
        return llm

    def plan(self, seasonal: bool, only_model: str | None = None, binary: bool = True) -> list[ThrottledLlm]:
        tier = self._tier(seasonal)
        models = tier.binary_round1 if binary else tier.all_forecasters
        return [self._chain(m) for m in ([only_model] if only_model else models)]

    def round2(self, seasonal: bool, used_models: list[str]) -> list[ThrottledLlm]:
        return [self._chain(m) for m in self._tier(seasonal).binary_round2]

    def quick_forecaster(self) -> ThrottledLlm:
        return self._chain(ensemble.OPUS_55)

    def any_quota_left(self) -> bool:
        return True

    def save(self) -> None:
        return None


def _credits_lineup() -> Lineup:
    helper = GeneralLlm(
        model=ensemble.FLASH_36,
        temperature=None,
        timeout=180,
        allowed_tries=3,
    )
    planner = CreditsPlanner()
    return Lineup(
        name="credits",
        llms={
            "default": planner.quick_forecaster(),
            "parser": helper,
            "summarizer": helper,
            "researcher": RESEARCHER,
        },
        research_reports_per_question=1,
        predictions_per_research_report=len(ensemble.TIERS["full"].all_forecasters),
        parser_validation_samples=2,
        summarize_research=True,
        free_only=False,
        test_only=False,
        planner=planner,
    )


_LINEUPS = {
    "free": _free_lineup,
    "gemini-free": _gemini_free_lineup,
    "credits": _credits_lineup,
}


def get_lineup(name: str | None = None) -> Lineup:
    """The active lineup, or `name` if given (Test Bot passes "free")."""
    lineup = _LINEUPS[name or ACTIVE_LINEUP]()
    if lineup.free_only:
        paid = [name for name in lineup.llm_model_names() if not is_free_model(name)]
        if paid:
            raise ValueError(f"Free lineup contains models that can be charged: {paid}")
    return lineup
