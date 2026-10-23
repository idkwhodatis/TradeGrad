"""Immutable, SDK-free research configuration, not a claim of broker realism.

Only implemented engine settings and objective definitions are accepted. Native
strategies still share the engine process; this contract cannot prove that their
lookback declaration or data use is truthful.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from datetime import date
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from .native_contract import NativeStrategyContract

Task = Literal["cn_time", "us_time", "cn_cross", "us_cross"]
Profile = Literal["source_compat", "corrected_explicit_config"]
Split = Literal["train", "validation", "holdout"]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[
    str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")
]


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


class FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, validate_default=True
    )


class VersionRef(FrozenModel):
    id: Identifier
    sha256: Sha256


class BenchmarkConfig(FrozenModel):
    definition_version: Literal["daily_close_observed_anchor.v1"] = (
        "daily_close_observed_anchor.v1"
    )
    dataset: VersionRef
    listing_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    price_field: Literal["close"] = "close"


class DateWindow(FrozenModel):
    start: date
    end: date

    @model_validator(mode="after")
    def nonempty(self):
        if self.start >= self.end:
            raise ValueError("Window must be nonempty and use [start, end)")
        if (self.end - self.start).days > 36525:
            raise ValueError("Window exceeds 100 years")
        return self

    def overlaps(self, other: DateWindow) -> bool:
        return self.start < other.end and other.start < self.end


class WindowSet(FrozenModel):
    train: DateWindow
    validation: DateWindow | None = None
    holdout: DateWindow | None = None

    @model_validator(mode="after")
    def chronological_disjoint(self):
        present = self.items()
        for (_, first), (_, second) in itertools.pairwise(present):
            if first.end > second.start:
                raise ValueError(
                    "Train, validation and holdout must be chronological, disjoint half-open windows"
                )
        return self

    def items(self) -> tuple[tuple[Split, DateWindow], ...]:
        return tuple(
            (name, value)
            for name in ("train", "validation", "holdout")
            if (value := getattr(self, name)) is not None
        )

    def get(self, split: Split) -> DateWindow:
        if (
            split not in ("train", "validation", "holdout")
            or (window := getattr(self, split)) is None
        ):
            raise ValueError(f"No configured {split} window")
        return window

    def optimization_windows(self) -> tuple[tuple[Split, DateWindow], ...]:
        """Named optimization splits only; use evaluation_windows for engine windows."""
        return tuple(
            (name, window) for name, window in self.items() if name != "holdout"
        )


class WarmupConfig(FrozenModel):
    sessions: int = Field(ge=0, le=252)
    history_policy: Literal["require", "source_cold_start"] = "require"
    lookback_contract: Literal["declared_seed_minimum"] = "declared_seed_minimum"


class SlippageConfig(FrozenModel):
    type: Literal["percent"] = "percent"
    value: float = Field(default=0.0005, ge=0, le=0.1)


class RiskConfig(FrozenModel):
    account_mode: Literal["cash", "margin"]
    enable_short_sell: bool

    @model_validator(mode="after")
    def margin_required(self):
        if self.enable_short_sell and self.account_mode != "margin":
            raise ValueError("Short selling requires margin account mode")
        return self


class MarketConfig(FrozenModel):
    market: Literal["CN", "US"]
    currency: Literal["CNY", "USD"]
    calendar: Literal["XSHG", "XNYS"]
    calendar_note: str = Field(min_length=1, max_length=500)
    timezone: Literal["Asia/Shanghai", "America/New_York"] | None
    lot_size: int | None = Field(default=None, ge=1, le=10000)
    initial_cash: float = Field(default=1_000_000, gt=0, le=1_000_000_000_000)
    commission_rate: float = Field(default=0.0005, ge=0, le=0.1)
    stamp_tax_rate: float = Field(ge=0, le=0.1)
    slippage: SlippageConfig = Field(default_factory=SlippageConfig)
    history_depth: Literal[252] = 252
    t_plus_one: bool
    risk_config: RiskConfig | None = None
    decision_timing: Literal["native_akquant"] = "native_akquant"
    assumptions: Literal["historical_demonstration_not_broker_terms"] = (
        "historical_demonstration_not_broker_terms"
    )

    @model_validator(mode="after")
    def supported_market_contract(self):
        cn = self.market == "CN"
        if self.currency != ("CNY" if cn else "USD") or self.calendar != (
            "XSHG" if cn else "XNYS"
        ):
            raise ValueError("Currency/calendar do not match the supported market")
        if self.timezone not in (None, "Asia/Shanghai" if cn else "America/New_York"):
            raise ValueError("Timezone does not match market")
        if self.lot_size not in (None, 100 if cn else 1):
            raise ValueError("Only the declared CN/US equity lot size is supported")
        if self.t_plus_one != cn:
            raise ValueError("Settlement behavior does not match the supported market")
        if cn and self.risk_config and self.risk_config.enable_short_sell:
            raise ValueError("CN short-selling is not certified by this adapter")
        return self

    @classmethod
    def for_market(
        cls, market: str, profile: Profile = "corrected_explicit_config"
    ) -> MarketConfig:
        if market not in ("CN", "US") or profile not in (
            "source_compat",
            "corrected_explicit_config",
        ):
            raise ValueError("Unsupported market/profile")
        cn, corrected = market == "CN", profile == "corrected_explicit_config"
        return cls(
            market=market,
            currency="CNY" if cn else "USD",
            calendar="XSHG" if cn else "XNYS",
            calendar_note=(
                "XSHG regular-equity schedule; Shenzhen uses a declared proxy."
                if cn
                else "XNYS regular-equity schedule; other supported US venues use a declared proxy."
            ),
            timezone=("Asia/Shanghai" if cn else "America/New_York")
            if corrected
            else None,
            lot_size=(100 if cn else 1) if corrected else None,
            stamp_tax_rate=0.001 if cn else 0.0,
            t_plus_one=cn,
            risk_config=None
            if cn
            else RiskConfig(account_mode="margin", enable_short_sell=True),
        )

    def engine_kwargs(self) -> dict:
        """A fresh, bounded mapping of settings actually consumed by AKQuant."""
        names = (
            "initial_cash",
            "commission_rate",
            "stamp_tax_rate",
            "slippage",
            "history_depth",
            "t_plus_one",
            "risk_config",
            "timezone",
            "lot_size",
        )
        values = self.model_dump(mode="json")
        return {name: values[name] for name in names if values[name] is not None}


class ObjectiveConfig(FrozenModel):
    windowing_definition: Literal["calendar_year_train_full_validation.v1"] = (
        "calendar_year_train_full_validation.v1"
    )
    definition_version: Literal["source_reward.v1"] = "source_reward.v1"
    metric_definition_version: Literal["source_metrics.v1"] = "source_metrics.v1"
    # Do not accept overrides until both evaluators actually consume them.
    ar_weight: Literal[0.5] = 0.5
    ar_reference: Literal[20] = 20
    mdd_reference: Literal[15] = 15
    freq_sharpness: Literal[1] = 1
    frequency_floor_version: Literal["source_market.v1"] = "source_market.v1"
    reward_rounding: Literal["source_metrics_before_reward"] = (
        "source_metrics_before_reward"
    )


class OptimizerConfig(FrozenModel):
    algorithm: Literal["trade_tgd"] = "trade_tgd"
    max_iterations: int = Field(default=1, ge=0, le=1000)
    sample_size: int = Field(default=3, ge=1, le=100)
    sample_method: Literal["power_law", "uniform", "top_k"] = "power_law"
    large_revision_prob: float = Field(default=0.7, ge=0, le=1)
    memory_window: int = Field(default=5, ge=1, le=1000)
    seed: int = Field(default=0, ge=0, le=2**32 - 1)
    worst_ratio: float = Field(default=0.3, gt=0, le=1)


class BudgetConfig(FrozenModel):
    max_model_calls: int = Field(default=10, ge=0, le=10000)
    max_tokens: int = Field(default=100000, ge=0, le=100000000)
    max_runtime_seconds: int = Field(default=300, ge=1, le=86400)
    max_repairs: int = Field(default=2, ge=0, le=20)


class ModelConfig(FrozenModel):
    provider: Literal["fixture", "openai", "openrouter", "openai-compatible"] = (
        "fixture"
    )
    model: Identifier = "deterministic-fixture-v1"
    # Retained as null for old fixture documents. No API may name an environment
    # variable, credential reference, endpoint, arbitrary parameter or secret.
    credential_ref: None = None
    profile_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")] | None = None
    profile_sha256: Sha256 | None = None
    temperature: float = Field(default=0, ge=0, le=2)
    max_output_tokens: int = Field(default=4096, ge=1, le=16384)

    @field_validator("model")
    @classmethod
    def no_credentials_or_urls(cls, value):
        if "://" in value or value.lower().startswith(("sk-", "sk_", "bearer", "eyj")):
            raise ValueError("Use an admitted model identifier, never a URL or secret")
        return value

    @model_validator(mode="after")
    def profile_contract(self):
        if self.provider == "fixture":
            if (
                self.model != "deterministic-fixture-v1"
                or self.temperature != 0
                or self.max_output_tokens != 4096
                or self.profile_id is not None
                or self.profile_sha256 is not None
            ):
                raise ValueError(
                    "Fixture model requires its deterministic identity and temperature 0"
                )
        elif self.profile_id is None or self.profile_sha256 is None:
            raise ValueError(
                "Online model requires an operator profile and immutable profile hash"
            )
        return self

    @model_serializer(mode="wrap")
    def historical_fixture_representation(self, serializer):
        values = serializer(self)
        if self.provider == "fixture":
            # V1 fixture bytes/hashes are immutable historical evidence. Do not
            # expand defaults when reading old runs, checkpoints or packages.
            for key in ("profile_id", "profile_sha256", "max_output_tokens"):
                values.pop(key, None)
        return values


class RunConfig(FrozenModel):
    schema_version: Literal["tradegrad.run.v1"] = "tradegrad.run.v1"
    execution_mode: Literal["seed_evaluation", "optimization"] = "seed_evaluation"
    task: Task
    profile: Profile = "corrected_explicit_config"
    dataset: VersionRef
    seed_strategy: VersionRef
    universe: VersionRef
    windows: WindowSet
    warmup: WarmupConfig
    market_profile: MarketConfig
    objective: ObjectiveConfig = Field(default_factory=ObjectiveConfig)
    optimizer: OptimizerConfig = Field(default_factory=OptimizerConfig)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    prompt_version: Literal["source_prompts.v1"] = "source_prompts.v1"
    environment_digest: Sha256
    benchmark: BenchmarkConfig | None = None
    strategy_contract: NativeStrategyContract | None = None

    @model_serializer(mode="wrap")
    def historical_native_representation(self, serializer):
        values = serializer(self)
        if self.strategy_contract is None:
            values.pop("strategy_contract", None)
        return values

    @model_validator(mode="after")
    def implemented_semantics(self):
        market = self.task[:2].upper()
        if self.benchmark is not None and self.market_profile.timezone is None:
            raise ValueError(
                "Benchmark comparison requires an explicit strategy timezone"
            )
        if self.market_profile.market != market:
            raise ValueError("Task and market profile must agree")
        if self.execution_mode == "optimization" and self.windows.validation is None:
            raise ValueError("Optimization requires a distinct validation window")
        minimum = (
            max(
                self.strategy_contract.lookback_bars, self.strategy_contract.warmup_bars
            )
            if self.strategy_contract is not None
            else 20
            if self.task.endswith("_time")
            else 3
        )
        if self.profile == "source_compat":
            if self.strategy_contract is not None:
                raise ValueError(
                    "source_compat cannot add strategy declarations or parameter overrides"
                )
            if (
                self.warmup.sessions != 0
                or self.warmup.history_policy != "source_cold_start"
            ):
                raise ValueError(
                    "source_compat preserves explicitly declared zero-history cold start"
                )
            if (
                self.market_profile.engine_kwargs()
                != MarketConfig.for_market(market, self.profile).engine_kwargs()
            ):
                raise ValueError("source_compat must preserve source market defaults")
        else:
            if (
                self.warmup.history_policy != "require"
                or self.warmup.sessions < minimum
            ):
                raise ValueError(
                    f"Corrected {self.task} requires at least {minimum} previous available sessions"
                )
            if (
                self.market_profile.timezone is None
                or self.market_profile.lot_size is None
            ):
                raise ValueError(
                    "Corrected profile requires explicit timezone and lot size"
                )
        requires_shorting = (
            self.strategy_contract.requires_shorting
            if self.strategy_contract is not None
            else market == "US"
        )
        if requires_shorting and not (
            self.market_profile.risk_config
            and self.market_profile.risk_config.enable_short_sell
        ):
            raise ValueError(
                "Strategy requires short-selling support; enable an admitted margin profile or choose a long-only strategy"
            )
        if self.warmup.sessions > self.market_profile.history_depth:
            raise ValueError("Warm-up exceeds pinned engine history depth")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.model_dump(mode="json"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_verified_json(cls, value: bytes | str, expected_sha256: str) -> RunConfig:
        parsed = cls.model_validate_json(value)
        if parsed.sha256 != expected_sha256:
            raise ValueError("Immutable RunConfig digest mismatch")
        return parsed


def evaluation_windows(config: RunConfig) -> tuple[dict[str, str], ...]:
    """Canonical independent engine runs: yearly train, whole validation, no holdout.

    The source loss accepts inclusive dates. Each returned [start,end) maps to
    the same source call using inclusive_end = end - one day. Crossing Jan 1
    starts a new account/engine run; a Jan 1 exclusive end creates no empty year.
    Full config identity is unchanged and pins this windowing definition.
    """
    config = RunConfig.model_validate(config.model_dump(mode="json"))
    train = config.windows.train
    cursor = train.start
    windows = []
    while cursor < train.end:
        boundary = (
            date(cursor.year + 1, 1, 1) if cursor.year < date.max.year else train.end
        )
        end = min(boundary, train.end)
        windows.append({"split": "train", "start": str(cursor), "end": str(end)})
        cursor = end
    if config.windows.validation is not None:
        validation = config.windows.validation
        windows.append(
            {
                "split": "validation",
                "start": str(validation.start),
                "end": str(validation.end),
            }
        )
    return tuple(windows)
