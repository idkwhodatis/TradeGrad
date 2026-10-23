"""Typed, bounded read contracts for persisted native research evidence."""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from packages.tradegrad_adapter.config import (
    BudgetConfig,
    MarketConfig,
    ModelConfig,
    ObjectiveConfig,
    OptimizerConfig,
    Profile,
    RunConfig,
    Task,
    WarmupConfig,
)

ResultSection = Literal[
    "equity",
    "drawdown",
    "cash",
    "margin",
    "positions",
    "orders",
    "fills",
    "closed_trades",
]
Scalar = str | int | float | bool | None


class ViewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class EvaluationWindow(ViewModel):
    split: Literal["train", "validation"]
    start: date
    end: date


class ResearchPreflightPublic(ViewModel):
    valid: Literal[True] = True
    config_sha256: str
    evaluation_windows: list[EvaluationWindow]
    is_synthetic: bool
    warnings: list[str]
    holdout_execution: Literal[False] = False


class ResearchDefaults(ViewModel):
    optimizer: OptimizerConfig = Field(default_factory=OptimizerConfig)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    objective: ObjectiveConfig = Field(default_factory=ObjectiveConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)


class ProfileDefault(ViewModel):
    task: Task
    profile: Profile
    market_profile: MarketConfig
    warmup: WarmupConfig


class SeedOptimizer(ViewModel):
    max_iterations: Literal[0] = 0


class SeedOnlyResearch(ViewModel):
    optimizer: SeedOptimizer = Field(default_factory=SeedOptimizer)


class ResearchLimits(ViewModel):
    max_active_runs_per_user: int
    max_active_imports_per_user: int
    max_upload_bytes: int
    max_artifact_bytes: int


class ModelProfilePublic(ViewModel):
    id: str
    label: str
    config: ModelConfig
    synthetic: bool
    request_timeout_seconds: float
    strategy_modes: list[Literal["native_akquant", "portable"]]


class ResearchCapabilities(ViewModel):
    environment_digest: str
    schema_version: Literal["tradegrad.run.v1"] = "tradegrad.run.v1"
    prompt_versions: list[Literal["source_prompts.v1"]] = ["source_prompts.v1"]
    model_providers: list[str] = ["fixture"]
    models: list[str] = ["deterministic-fixture-v1"]
    model_profiles: list[ModelProfilePublic] = Field(default_factory=list)
    execution_modes: list[Literal["optimization"]] = ["optimization"]
    seed_only_research: SeedOnlyResearch = Field(default_factory=SeedOnlyResearch)
    live_model_calls: bool = False
    holdout_execution: Literal[False] = False
    checkpoint_resume: Literal[True] = True
    tasks: list[Task] = ["cn_time", "us_time", "cn_cross", "us_cross"]
    profiles: list[Profile] = ["source_compat", "corrected_explicit_config"]
    sample_methods: list[Literal["power_law", "uniform", "top_k"]] = [
        "power_law",
        "uniform",
        "top_k",
    ]
    preflight: Literal[True] = True
    result_query: Literal[True] = True
    report_download: Literal[True] = True
    compare_max_evaluations: Literal[4] = 4
    portable_execution: Literal[False] = False
    defaults: ResearchDefaults = Field(default_factory=ResearchDefaults)
    profile_defaults: list[ProfileDefault]
    limits: ResearchLimits


class ResultMetric(ViewModel):
    name: str
    value: int | float | None
    unit: str
    definition_version: str
    calculation_basis: str
    unavailable_reason: str | None


class ResultColumn(ViewModel):
    name: str
    dtype: str
    unit: str


class ResultTable(ViewModel):
    columns: list[ResultColumn]
    rows: list[list[Scalar]]
    total_rows: int
    filtered_rows: int
    returned_rows: int
    offset: int
    limit: int
    downsampled: bool
    sampling: Literal["none", "min_max"]
    filter_status: Literal[
        "not_requested", "applied", "portfolio_series_unfiltered_by_listing"
    ]


class ResultCapabilities(ViewModel):
    equity: bool
    drawdown: bool
    positions: bool
    orders: bool
    fills: bool
    closed_trades: bool
    fees_aggregate: bool
    fee_component_breakdown: Literal[False]
    benchmark: bool
    observable_order_trace: bool
    internal_decisions: Literal[False]
    trusted_ledger: Literal[False]


class ResultFees(ViewModel):
    unit: Literal["CNY", "USD"]
    definition_version: str
    total_from_fills: int | float
    native_total_commission: int | float
    calculation_basis: str
    commission_stamp_transfer_breakdown: None = None
    breakdown_status: Literal["unsupported_by_execution_report"]
    slippage_cost: None = None
    slippage_cost_status: Literal["not_separately_reported_by_native_engine"]


class ResultBenchmark(ViewModel):
    status: Literal["not_configured", "available"]
    series: dict[str, JsonValue] | None = None
    reason: str
    definition_version: str | None = None
    input: dict[str, JsonValue] | None = None
    normalization: dict[str, JsonValue] | None = None
    metrics: list[dict[str, JsonValue]] = Field(default_factory=list)
    series_total_rows: int = 0
    series_filtered_rows: int = 0
    series_returned_rows: int = 0
    downsampled: bool = False
    sampling: Literal["none", "uniform"] = "none"
    filter_scope: str | None = None


class ResultComparisonItem(ViewModel):
    evaluation_id: UUID
    run_id: UUID
    candidate_key: str
    code_sha256: str
    config_sha256: str
    artifact_sha256: str
    split: Literal["train", "validation"]
    status: Literal["succeeded"] = "succeeded"
    window: EvaluationWindow
    config: RunConfig
    currency: Literal["CNY", "USD"]
    price_definition: dict[str, JsonValue]
    result_trust: Literal["native_strategy_and_engine_share_process"]
    is_synthetic: bool
    metrics: list[ResultMetric]


class ResultQueryPublic(ResultComparisonItem):
    schema_version: Literal["tradegrad.result-query.v1"] = "tradegrad.result-query.v1"
    tables: dict[ResultSection, ResultTable]
    capabilities: ResultCapabilities
    fees: ResultFees
    benchmark: ResultBenchmark
    warnings: list[str]
    listing_id: UUID | None = None
    start: date | None = None
    end: date | None = None


class ResultCompareCreate(ViewModel):
    evaluation_ids: list[UUID] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def distinct(self):
        if len(set(self.evaluation_ids)) != len(self.evaluation_ids):
            raise ValueError("Choose distinct evaluations")
        return self


class DifferenceValue(ViewModel):
    evaluation_id: UUID
    value: JsonValue


class ComparisonDifference(ViewModel):
    field: str
    affects_comparability: bool
    values: list[DifferenceValue]


class ResultComparePublic(ViewModel):
    comparable: bool
    differences: list[ComparisonDifference]
    evaluations: list[ResultComparisonItem]
    ranking: None = None
    warnings: list[str]


class ResearchUsagePublic(ViewModel):
    run_id: UUID
    scope: Literal["persisted_logical_model_calls"] = "persisted_logical_model_calls"
    calls: int
    adopted_calls: int
    input_tokens: int | None
    output_tokens: int | None
    charged_tokens: int
    known_usage_calls: int
    unknown_usage_calls: int
    cost: None = None
    cost_status: Literal["unavailable"] = "unavailable"
    usage_sources: list[str]
