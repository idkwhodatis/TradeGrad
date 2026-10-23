"""Bounded, typed projections; original result bytes remain authoritative."""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue

from backend.app.data_schemas import PreviewBar
from backend.app.research_view_schemas import ViewModel


class ExplorationContext(ViewModel):
    mode: Literal[
        "native_research", "native_final", "portable_research", "portable_final"
    ]
    evaluation_id: UUID
    run_id: UUID
    artifact_sha256: str
    dataset_id: UUID
    dataset_sha256: str
    evaluation_data_sha256: str | None = None
    split: Literal["train", "validation", "holdout"]
    start: date
    end: date
    timezone: str
    result_trust: str
    ledger_origin: str
    selection_id: UUID | None = None
    listings: list[dict[str, str]]


class ReturnMonth(ViewModel):
    month: str
    value: float | None
    status: Literal[
        "observed_month", "partial_period", "missing_observations", "invalid_base"
    ]
    first_observation: date | None
    last_observation: date | None
    baseline_date: date | None
    observations: int
    reason: str


class ReturnBin(ViewModel):
    lower: float
    upper: float
    count: int
    upper_inclusive: bool


class ReturnAnalysis(ViewModel):
    definition_version: Literal["tradegrad.observed-equity-returns.v1"] = (
        "tradegrad.observed-equity-returns.v1"
    )
    unit: Literal["fraction"] = "fraction"
    scope: Literal["full_persisted_evaluation_portfolio"] = (
        "full_persisted_evaluation_portfolio"
    )
    monthly_definition: str
    distribution_definition: str
    coverage_status: Literal[
        "exchange_session_completeness_not_verified",
        "matches_pinned_session_dates",
        "missing_pinned_sessions",
    ] = "exchange_session_completeness_not_verified"
    expected_sessions: int | None = None
    missing_sessions: list[date] = Field(default_factory=list)
    unexpected_observations: list[date] = Field(default_factory=list)
    warnings: list[str]
    months: list[ReturnMonth]
    bins: list[ReturnBin]
    observations: int
    valid_returns: int
    excluded_returns: int
    collapsed_intraday_observations: int
    status: Literal["available", "insufficient_observations"]


class TradeMarker(ViewModel):
    id: str
    row_index: int
    session_date: date
    timestamp: str
    symbol: str
    side: str
    price: float
    quantity: float
    commission: float
    order_id: str
    on_price_bar: bool
    listing_id: str | None = None
    fill_id: str | None = None
    native_timestamp_ns: str | None = None
    economic_reference_time: str | None = None
    economic_reference: str | None = None


class ResultReturnAnalysis(ReturnAnalysis):
    context: ExplorationContext


class IndicatorPoint(ViewModel):
    session_date: date
    value: float | None


class IndicatorSeries(ViewModel):
    instance_id: str
    name: str
    symbol: str
    pane: int
    render_type: str
    unit: str
    status: Literal["available", "unsupported_render", "no_observations"]
    reason: str
    metadata: dict[str, JsonValue]
    points: list[IndicatorPoint]
    total_points: int
    warmup_excluded: int
    duplicate_session_points: int


class PriceReplay(ViewModel):
    schema_version: Literal["tradegrad.price-replay.v1"] = "tradegrad.price-replay.v1"
    context: ExplorationContext
    evaluation_id: UUID
    artifact_sha256: str
    dataset_sha256: str
    listing_id: UUID
    symbol: str
    timezone: str
    start: date
    end: date
    bars: list[PreviewBar]
    bar_count: int
    offset: int
    limit: int
    markers: list[TradeMarker]
    marker_count: int
    markers_truncated: bool
    indicators_status: str
    indicators_reason: str
    indicators: list[IndicatorSeries]
    warnings: list[str]
