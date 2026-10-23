"""I0 immutable leaf-evaluation evidence contracts.

Descriptions are not executable rules. Every absent observation has a reason;
false, zero and empty collections are only used when actually observed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "tradegrad.decision-trace.v2"
SPEC_VERSION = "tradegrad.strategy-spec.v1"
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=200)]
EvidenceSource = Literal[
    "llm_explanation",
    "static_inference",
    "strategy_emitted",
    "adapter_observed",
    "engine_observed",
    "experiment_result",
]
PredicateState = Literal["true", "false", "not_evaluated", "unknown", "not_sampled"]
ValueState = Literal[
    "valid",
    "warmup_insufficient",
    "input_missing",
    "calculation_error",
    "not_collected",
    "not_sampled",
]
CoverageState = Literal[
    "complete", "partial", "not_collected", "not_sampled", "unsupported"
]
NonTradeReason = Literal[
    "outside_universe",
    "feature_unavailable",
    "eligibility_filter",
    "rank_not_selected",
    "holding_retained",
    "position_slots",
    "portfolio_constraint",
    "cash_constraint",
    "timing_not_triggered",
    "order_rejected",
    "order_unfilled",
    "partial_fill",
    "not_evaluated",
    "not_collected",
    "not_sampled",
    "selected",
    "exit",
]


def canonical_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class DateRange(Contract):
    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self):
        if self.start >= self.end:
            raise ValueError("Date ranges are nonempty and half-open")
        return self


class SourceRef(Contract):
    code_sha256: Sha256
    file: str = Field(min_length=1, max_length=300)
    symbol: str = Field(min_length=1, max_length=200)
    line_start: int = Field(ge=1, le=1_000_000)
    line_end: int = Field(ge=1, le=1_000_000)

    @model_validator(mode="after")
    def safe_location(self):
        path = PurePosixPath(self.file)
        if path.is_absolute() or ".." in path.parts or "\\" in self.file:
            raise ValueError("Source locations must be relative version-local files")
        if self.line_end < self.line_start:
            raise ValueError("Source line range is inverted")
        return self


class Feature(Contract):
    feature_id: Identifier
    instrument_id: Identifier | None = None
    value: float | int | str | bool | None
    validity: ValueState
    unit: str = Field(min_length=1, max_length=80)
    calculation_window: str = Field(min_length=1, max_length=200)
    information_cutoff: datetime
    missing_reason: str | None = Field(default=None, max_length=500)
    source: EvidenceSource
    source_ref: SourceRef | None = None

    @model_validator(mode="after")
    def truthful(self):
        if self.information_cutoff.tzinfo is None:
            raise ValueError("Feature information cutoff requires a timezone")
        if self.validity == "valid":
            if self.value is None or self.missing_reason is not None:
                raise ValueError(
                    "A valid feature needs an observed value, not a missing reason"
                )
        elif self.value is not None or not self.missing_reason:
            raise ValueError("Missing features need null value and an explicit reason")
        return self


class Predicate(Contract):
    condition_id: Identifier
    instrument_id: Identifier | None = None
    operator: Literal[
        "gt", "gte", "lt", "lte", "eq", "neq", "in", "and", "or", "not", "custom"
    ]
    operands: list[Any] | None
    threshold: Any = None
    state: PredicateState
    missing_reason: str | None = Field(default=None, max_length=500)
    source: EvidenceSource
    source_ref: SourceRef | None = None

    @model_validator(mode="after")
    def truthful(self):
        canonical_bytes(self.operands)
        canonical_bytes(self.threshold)
        if self.state in {"true", "false"}:
            if self.operands is None or self.missing_reason is not None:
                raise ValueError("Evaluated predicates require actual operands")
        elif not self.missing_reason:
            raise ValueError("Unobserved predicates require a distinct missing reason")
        return self


class CandidateObservation(Contract):
    instrument_id: Identifier
    universe_member: bool | None
    eligibility: Literal["eligible", "ineligible", "unknown", "not_sampled"]
    elimination_stage: Identifier | None = None
    reason: NonTradeReason
    reason_detail: str | None = Field(default=None, max_length=500)
    score: float | None
    score_validity: ValueState
    rank: int | None = Field(default=None, ge=1)
    ranking_direction: Literal["ascending", "descending"] | None = None
    tie_break: str | None = Field(default=None, max_length=200)
    selected: bool | None
    prior_holding: bool | None
    source: EvidenceSource
    source_ref: SourceRef | None = None

    @model_validator(mode="after")
    def truthful(self):
        if (self.score_validity == "valid") != (self.score is not None):
            raise ValueError("Score availability and validity disagree")
        if self.rank is not None and (
            self.ranking_direction is None or not self.tie_break
        ):
            raise ValueError("Observed ranks must declare direction and tie-breaking")
        if self.universe_member is False and self.reason != "outside_universe":
            raise ValueError(
                "Outside-universe observations cannot claim a ranking outcome"
            )
        return self


class PortfolioState(Contract):
    equity: float | None = None
    cash: float | None
    positions: list[dict[str, Any]] | None
    open_orders: list[dict[str, Any]] | None
    strategy_state: dict[str, Any] | None = None
    coverage: CoverageState
    missing_reason: str | None = Field(default=None, max_length=500)
    source: EvidenceSource

    @model_validator(mode="after")
    def truthful(self):
        canonical_bytes(self.model_dump(mode="json"))
        if self.coverage == "complete" and any(
            value is None for value in (self.cash, self.positions, self.open_orders)
        ):
            raise ValueError(
                "Complete pre-state cannot replace missing account data with zeros"
            )
        if self.coverage != "complete" and not self.missing_reason:
            raise ValueError("Partial portfolio state needs a reason")
        return self


class OrderLink(Contract):
    logical_order_id: Identifier
    child_order_ids: list[Identifier]
    fill_ids: list[Identifier]
    instrument_id: Identifier
    submitted_at: datetime | None
    fill_times: list[datetime]
    status: Literal[
        "intended",
        "accepted",
        "rejected",
        "unfilled",
        "partially_filled",
        "filled",
        "cancelled",
        "unknown",
    ]
    rejection_reason: str | None = Field(default=None, max_length=500)
    source: EvidenceSource

    @model_validator(mode="after")
    def timestamps(self):
        times = ([self.submitted_at] if self.submitted_at else []) + self.fill_times
        if any(value.tzinfo is None for value in times):
            raise ValueError("Execution times require a timezone")
        if self.submitted_at and any(
            value < self.submitted_at for value in self.fill_times
        ):
            raise ValueError("A fill cannot precede its linked order")
        if self.status == "rejected" and not self.rejection_reason:
            raise ValueError("Rejected order needs the actual rejection reason")
        return self


class StageObservation(Contract):
    stage_id: Identifier
    coverage: CoverageState
    missing_reason: str | None = Field(default=None, max_length=500)
    features: list[Feature] = Field(default_factory=list)
    predicates: list[Predicate] = Field(default_factory=list)
    candidates: list[CandidateObservation] = Field(default_factory=list)
    raw_targets: list[dict[str, Any]] | None = None
    constrained_plan: list[dict[str, Any]] | None = None
    order_links: list[OrderLink] = Field(default_factory=list)

    @model_validator(mode="after")
    def truthful(self):
        canonical_bytes(self.model_dump(mode="json"))
        if self.coverage != "complete" and not self.missing_reason:
            raise ValueError("Incomplete stages need an explicit coverage reason")
        return self


class DecisionRecord(Contract):
    decision_id: Identifier
    data_event_id: Identifier
    event_seq: int = Field(ge=0)
    scope: Literal["portfolio", "instrument"]
    instrument_id: Identifier | None = None
    callback_phase: Identifier
    information_cutoff: datetime
    decision_time: datetime
    market_timezone: str
    pre_state: PortfolioState | None
    pre_state_missing_reason: str | None = Field(default=None, max_length=500)
    stages: list[StageObservation]

    @field_validator("market_timezone")
    @classmethod
    def timezone(cls, value):
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def truthful(self):
        if self.information_cutoff.tzinfo is None or self.decision_time.tzinfo is None:
            raise ValueError("Decision times require a timezone")
        if self.information_cutoff > self.decision_time:
            raise ValueError("Decision inputs cannot include later information")
        if self.scope == "instrument" and not self.instrument_id:
            raise ValueError("Instrument decisions require an instrument identity")
        if self.scope == "portfolio" and self.instrument_id is not None:
            raise ValueError(
                "Portfolio decisions cannot be split into per-instrument decisions"
            )
        if self.pre_state is None and not self.pre_state_missing_reason:
            raise ValueError("Absent pre-state needs an explicit reason")
        ids = [stage.stage_id for stage in self.stages]
        if len(ids) != len(set(ids)):
            raise ValueError("Stages must be unique within a decision")
        for stage in self.stages:
            if any(
                feature.information_cutoff > self.information_cutoff
                for feature in stage.features
            ):
                raise ValueError(
                    "Feature cutoff exceeds the recorded decision information cutoff"
                )
        return self

    def pairing_key(self) -> tuple[str, str, str, int, str | None]:
        """Pair across executions without using random IDs or date-only joins."""
        return (
            self.data_event_id,
            self.callback_phase,
            self.scope,
            self.event_seq,
            self.instrument_id,
        )


class CaptureConfig(Contract):
    mode: Literal["off", "summary", "research"] = "summary"
    observation_window: DateRange | None = None
    stage_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    instrument_ids: list[Identifier] = Field(default_factory=list, max_length=10000)
    max_decisions: int = Field(default=10000, ge=1, le=1000000)
    max_detail_rows: int = Field(default=2000000, ge=1, le=20000000)
    max_uncompressed_bytes: int = Field(default=268435456, ge=1024, le=1073741824)


class EvaluationBinding(Contract):
    run_id: UUID
    execution_attempt_id: UUID
    evaluation_id: UUID
    candidate_id: UUID | None
    strategy_version_id: UUID
    code_sha256: Sha256
    dataset_version_id: UUID
    dataset_sha256: Sha256
    universe_version_id: UUID | None
    universe_sha256: Sha256 | None
    execution_config_sha256: Sha256
    engine_dependencies_sha256: Sha256
    result_manifest_sha256: Sha256

    @model_validator(mode="after")
    def universe_pair(self):
        if (self.universe_version_id is None) != (self.universe_sha256 is None):
            raise ValueError("Universe ID/hash must be bound together")
        return self


class ArtifactRef(Contract):
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]+$", max_length=200)
    sha256: Sha256
    size_bytes: int = Field(ge=0)
    rows: int = Field(ge=0)
    media_type: Literal["application/json", "application/vnd.apache.parquet"]
    date_range: DateRange | None = None


class TraceManifest(Contract):
    schema_version: Literal["tradegrad.decision-trace.v2"] = SCHEMA_VERSION
    binding: EvaluationBinding
    capture: CaptureConfig
    coverage: CoverageState
    missing_reasons: list[str] = Field(max_length=100)
    decisions_seen: int = Field(ge=0)
    decisions_saved: int = Field(ge=0)
    detail_rows_seen: int = Field(ge=0)
    detail_rows_saved: int = Field(ge=0)
    artifacts: list[ArtifactRef]
    stage_coverage: dict[str, CoverageState] = Field(max_length=100)
    source: EvidenceSource
    trust_boundary: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def truthful(self):
        if (
            self.decisions_saved > self.decisions_seen
            or self.detail_rows_saved > self.detail_rows_seen
        ):
            raise ValueError("Saved observation counts cannot exceed observed counts")
        if self.coverage != "complete" and not self.missing_reasons:
            raise ValueError("Incomplete capture must disclose its reason")
        if self.coverage == "complete" and (
            self.missing_reasons
            or any(value != "complete" for value in self.stage_coverage.values())
        ):
            raise ValueError(
                "Complete coverage cannot contain missing reasons or incomplete stages"
            )
        if self.coverage == "complete" and (
            self.decisions_saved != self.decisions_seen
            or self.detail_rows_saved != self.detail_rows_seen
        ):
            raise ValueError("Truncated observations cannot claim complete coverage")
        return self


class Rule(Contract):
    rule_id: Identifier
    category: Literal[
        "universe",
        "input",
        "feature",
        "filter",
        "ranking",
        "allocation",
        "exit",
        "execution",
    ]
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    parameters: dict[str, Any]
    units: dict[str, str]
    inputs: list[str]
    outputs: list[str]
    source_ref: SourceRef
    source: EvidenceSource
    stage_id: Identifier | None
    mapping: Literal["controlled", "human_confirmed", "inferred", "unmapped"]


class StrategySpec(Contract):
    schema_version: Literal["tradegrad.strategy-spec.v1"] = SPEC_VERSION
    code_sha256: Sha256
    template_id: Literal["daily_timing", "cross_sectional"] | None
    rules: list[Rule]
    edges: list[tuple[str, str]]
    coverage: CoverageState
    missing_reasons: list[str] = Field(max_length=100)
    revision: int = Field(ge=1)
    explanation_model: str | None = None
    prompt_version: str | None = None

    @model_validator(mode="after")
    def bound(self):
        ids = [rule.rule_id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("Rule IDs must be unique")
        if any(rule.source_ref.code_sha256 != self.code_sha256 for rule in self.rules):
            raise ValueError("Rule anchors must bind the exact described code")
        if any(left not in ids or right not in ids for left, right in self.edges):
            raise ValueError("Rule graph cannot link unknown rules")
        if self.template_id is None and self.edges:
            raise ValueError("Arbitrary source cannot claim a controlled rule graph")
        if self.coverage != "complete" and not self.missing_reasons:
            raise ValueError("Incomplete descriptions must disclose their gaps")
        return self


class EvidenceRef(Contract):
    kind: Literal[
        "rule", "decision", "comparison", "controlled_experiment", "evaluation", "code"
    ]
    strategy_version_id: UUID
    candidate_id: UUID | None = None
    code_sha256: Sha256
    evaluation_id: UUID | None = None
    execution_attempt_id: UUID | None = None
    result_manifest_sha256: Sha256 | None = None
    trace_manifest_sha256: Sha256 | None = None
    decision_id: Identifier | None = None
    instrument_id: Identifier | None = None
    stage_id: Identifier | None = None
    rule_id: Identifier | None = None
    spec_revision: int | None = Field(default=None, ge=1)
    spec_sha256: Sha256 | None = None
    comparison_id: UUID | None = None
    controlled_experiment_id: UUID | None = None
    source_ref: SourceRef | None = None

    @model_validator(mode="after")
    def exact(self):
        if self.evaluation_id is not None and (
            self.execution_attempt_id is None or self.result_manifest_sha256 is None
        ):
            raise ValueError(
                "Evaluation evidence requires attempt and immutable manifest"
            )
        if self.decision_id is not None and (
            self.evaluation_id is None or self.trace_manifest_sha256 is None
        ):
            raise ValueError(
                "Decision evidence requires leaf evaluation and exact trace manifest"
            )
        if self.source_ref and self.source_ref.code_sha256 != self.code_sha256:
            raise ValueError("Evidence cannot silently retarget a source anchor")
        if self.kind == "rule" and (
            self.rule_id is None
            or self.spec_revision is None
            or self.spec_sha256 is None
        ):
            raise ValueError(
                "Rule evidence requires exact rule, spec revision and hash"
            )
        if self.kind == "decision" and not self.decision_id:
            raise ValueError("Decision reference needs a decision ID")
        if self.kind == "comparison" and self.comparison_id is None:
            raise ValueError("Comparison reference needs an exact comparison ID")
        if (
            self.kind == "controlled_experiment"
            and self.controlled_experiment_id is None
        ):
            raise ValueError(
                "Controlled experiment reference needs an exact experiment ID"
            )
        if self.kind == "evaluation" and self.evaluation_id is None:
            raise ValueError("Evaluation reference needs a physical evaluation ID")
        return self


def legacy_coverage() -> dict:
    """A compatibility DTO, never manufactured observations for old artifacts."""
    return {
        "schema_version": SCHEMA_VERSION,
        "coverage": "not_collected",
        "missing_reasons": [
            "This immutable result predates structured research capture."
        ],
        "features": None,
        "predicates": None,
        "candidates": None,
        "pre_state": None,
    }
