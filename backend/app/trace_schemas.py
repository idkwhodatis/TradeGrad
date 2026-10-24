"""Typed, immutable evidence projections used to generate the browser client."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from packages.research_trace.contracts import (
    CandidateObservation,
    CoverageState,
    EvaluationBinding,
    Feature,
    OrderLink,
    PortfolioState,
    Predicate,
    Sha256,
    TraceManifest,
)


class TraceView(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TraceContext(TraceView):
    evaluation_id: UUID
    execution_attempt_id: UUID
    result_manifest_sha256: Sha256
    trace_manifest_sha256: Sha256 | None
    status: Literal["available", "not_collected"]
    missing_reason: str | None = None
    binding: EvaluationBinding | None = None


class TraceManifestPublic(TraceView):
    context: TraceContext
    manifest: TraceManifest | None


class DecisionIndex(TraceView):
    decision_id: str
    session_date: date
    decision_time: datetime
    information_cutoff: datetime
    event_seq: int
    data_event_id: str
    scope: Literal["portfolio", "instrument"]
    instrument_id: str | None
    callback_phase: str
    market_timezone: str
    stage_ids: list[str]


class DecisionPage(TraceView):
    context: TraceContext
    items: list[DecisionIndex] | None
    total: int | None
    offset: int
    limit: int


class DecisionHeader(TraceView):
    session_date: date
    decision_id: str
    data_event_id: str
    event_seq: int
    scope: Literal["portfolio", "instrument"]
    instrument_id: str | None
    callback_phase: str
    information_cutoff: datetime
    decision_time: datetime
    market_timezone: str
    pre_state: PortfolioState | None
    pre_state_missing_reason: str | None


class TraceStage(TraceView):
    stage_id: str
    coverage: CoverageState
    missing_reason: str | None
    raw_targets: list[dict] | None
    constrained_plan: list[dict] | None
    order_links: list[OrderLink]
    features_saved: int = Field(ge=0)
    predicates_saved: int = Field(ge=0)
    candidates_saved: int = Field(ge=0)


class FeatureRow(TraceView):
    stage_id: str
    ordinal: int = Field(ge=0, strict=True)
    observation: Feature


class PredicateRow(TraceView):
    stage_id: str
    ordinal: int = Field(ge=0, strict=True)
    observation: Predicate


class CandidateRow(TraceView):
    stage_id: str
    ordinal: int = Field(ge=0, strict=True)
    observation: CandidateObservation


class FeaturePage(TraceView):
    items: list[FeatureRow] | None
    total: int | None
    offset: int
    limit: int


class PredicatePage(TraceView):
    items: list[PredicateRow] | None
    total: int | None
    offset: int
    limit: int


class DecisionDetail(TraceView):
    context: TraceContext
    decision: DecisionHeader | None
    stages: list[TraceStage] | None
    features: FeaturePage
    predicates: PredicatePage
    instrument_id: str | None
    stage_id: str | None


class CandidatePage(TraceView):
    context: TraceContext
    decision_id: str
    items: list[CandidateRow] | None
    total: int | None
    offset: int
    limit: int
