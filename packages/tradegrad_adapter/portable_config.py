"""Immutable portable research inputs, deliberately not native RunConfig.

Only the reviewed daily cash-long-only profile is admitted. Configuration is
identity, not an isolation, market-data or cross-framework certification.
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Literal

from pydantic import Field, field_validator, model_validator

from packages.strategy_sdk import StateSnapshot, canonical_json, parse_json
from runtimes.portable_engine.runtime import reviewed_profile

from .config import (
    BudgetConfig,
    FrozenModel,
    ModelConfig,
    OptimizerConfig,
    Sha256,
    Task,
    VersionRef,
    WarmupConfig,
    WindowSet,
)


class PortableRiskConfig(FrozenModel):
    account_mode: Literal["cash"] = "cash"
    enable_short_sell: Literal[False] = False

    @field_validator("enable_short_sell", mode="before")
    @classmethod
    def exact_false(cls, value):
        if value is not False:
            raise ValueError("Portable short-selling flag must be explicit false")
        return value


class PortableSlippageConfig(FrozenModel):
    type: Literal["percent"] = "percent"
    value: Literal[0.0005] = 0.0005


class PortableEngineProfile(FrozenModel):
    profile_version: Literal["akquant.portable.cash-daily.controller-t1.v3"]
    engine: Literal["akquant"]
    engine_version: Literal["0.3.27"]
    market: Literal["CN", "US"]
    initial_cash: float = Field(gt=0, le=1e12, strict=True)
    commission_rate: Literal[0.0005]
    stamp_tax_rate: float
    transfer_fee_rate: Literal[0.0]
    min_commission: Literal[0.0]
    slippage: PortableSlippageConfig
    volume_limit_pct: Literal[0.25]
    t_plus_one: Literal[False]
    engine_settlement_mode: Literal["T+0"]
    application_settlement_enforcement: Literal[
        "trusted_controller_daily_next_open_inventory.v1"
    ]
    application_t_plus_one: bool = Field(strict=True)
    initial_positions: Literal["flat"]
    policy_callback: Literal["complete_daily_cross_section_after_close"]
    intraday_policy: Literal["unsupported"]
    external_funding: Literal["unsupported"]
    corporate_actions: Literal["reject_window_with_actions"]
    limit_execution: Literal["reject_non_open_or_limit_breaching_native_fill"]
    lot_size: int = Field(strict=True)
    timezone: Literal["Asia/Shanghai", "America/New_York"]
    risk_config: PortableRiskConfig
    fill_policy: Literal["NextOpen"]
    time_in_force: Literal["GTC"]
    sizing_price: Literal["decision_close"]
    sizing_equity: Literal["decision_close_native_equity"]
    cash_budget: Literal["available_cash_without_anticipated_sale_proceeds"]
    sell_budget: Literal["decision_close_holdings_less_pending_sells"]
    sellability_observation: Literal["next_open_unreserved_holdings"]
    unsettled_next_open_sell: Literal["admit_after_close_with_controller_t1"]
    pending_rebalance: Literal["skip_until_all_native_legs_terminal"]
    final_liquidation: Literal[False]

    @model_validator(mode="after")
    def reviewed_only(self):
        if self.model_dump(mode="json") != reviewed_profile(
            self.market, self.initial_cash
        ):
            raise ValueError(
                "Portable execution requires the exact reviewed engine profile"
            )
        return self

    @classmethod
    def for_market(cls, market: str, initial_cash: float = 1_000_000):
        return cls.model_validate(reviewed_profile(market, initial_cash))

    @property
    def sha256(self):
        return hashlib.sha256(canonical_json(self)).hexdigest()


class PortableObjectiveConfig(FrozenModel):
    windowing_definition: Literal["calendar_year_train_full_validation.v1"] = (
        "calendar_year_train_full_validation.v1"
    )
    definition_version: Literal["portable_return_drawdown.v1"] = (
        "portable_return_drawdown.v1"
    )
    metric_definition_version: Literal["portable_native_summary.v1"] = (
        "portable_native_summary.v1"
    )
    reward_formula: Literal["total_return_pct - max_drawdown_pct"] = (
        "total_return_pct - max_drawdown_pct"
    )


class PortableRunConfig(FrozenModel):
    schema_version: Literal["tradegrad.portable.run.v1"] = "tradegrad.portable.run.v1"
    evaluation_mode: Literal["portable"] = "portable"
    execution_mode: Literal["seed_evaluation", "optimization"] = "seed_evaluation"
    task: Task
    profile: Literal["corrected_explicit_config"] = "corrected_explicit_config"
    strategy_kind: Literal["portfolio", "event"]
    dataset: VersionRef
    seed_strategy: VersionRef
    seed_manifest: VersionRef
    seed_parameters: VersionRef
    universe: VersionRef
    windows: WindowSet
    warmup: WarmupConfig
    initial_cash: float = Field(default=1_000_000, gt=0, le=1e12, strict=True)
    initial_state: StateSnapshot
    engine_profile: PortableEngineProfile | None = None
    objective: PortableObjectiveConfig = Field(default_factory=PortableObjectiveConfig)
    optimizer: OptimizerConfig = Field(
        default_factory=lambda: OptimizerConfig(max_iterations=0)
    )
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    prompt_version: Literal["portable_business_rules.v1"] = "portable_business_rules.v1"
    environment_digest: Sha256
    benchmark: None = None
    conformance: Literal["interface_only"] = "interface_only"
    external_framework_equivalence: Literal["not_certified"] = "not_certified"

    @model_validator(mode="after")
    def portable_semantics(self):
        if self.strategy_kind != (
            "event" if self.task.endswith("_time") else "portfolio"
        ):
            raise ValueError(
                "Portable event tasks use _time and portfolio tasks use _cross"
            )
        if (
            self.execution_mode == "seed_evaluation"
            and self.optimizer.max_iterations != 0
        ):
            raise ValueError(
                "Portable seed evaluation requires zero optimizer iterations"
            )
        if self.execution_mode == "optimization" and self.windows.validation is None:
            raise ValueError("Optimization requires a distinct validation window")
        if self.warmup.history_policy != "require" or self.warmup.sessions < 1:
            raise ValueError("Portable research requires explicit pre-window warmup")
        if self.model.provider != "fixture":
            raise ValueError(
                "This portable research deployment admits fixture models only"
            )
        expected = PortableEngineProfile.for_market(self.market, self.initial_cash)
        if self.engine_profile is None:
            object.__setattr__(self, "engine_profile", expected)
        elif self.engine_profile != expected:
            raise ValueError(
                "Portable engine profile must match immutable market and cash"
            )
        return self

    @property
    def market(self) -> str:
        return self.task[:2].upper()

    def canonical_bytes(self) -> bytes:
        return canonical_json(self)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_verified_json(cls, value: bytes | str, expected_sha256: str):
        # The SDK parser rejects duplicate keys and bounded/non-finite JSON first.
        parsed = cls.model_validate_json(canonical_json(parse_json(value)))
        if parsed.sha256 != expected_sha256:
            raise ValueError("Immutable portable RunConfig digest mismatch")
        return parsed


def portable_evaluation_windows(
    config: PortableRunConfig,
) -> tuple[dict[str, str], ...]:
    """Yearly train and full validation only; full/partial holdout is never admitted."""
    config = PortableRunConfig.model_validate_json(config.canonical_bytes())
    train = config.windows.train
    cursor, windows = train.start, []
    while cursor < train.end:
        boundary = date(cursor.year + 1, 1, 1) if cursor.year < 9999 else train.end
        end = min(boundary, train.end)
        windows.append({"split": "train", "start": str(cursor), "end": str(end)})
        cursor = end
    if config.windows.validation is not None:
        window = config.windows.validation
        windows.append(
            {"split": "validation", "start": str(window.start), "end": str(window.end)}
        )
    return tuple(windows)
