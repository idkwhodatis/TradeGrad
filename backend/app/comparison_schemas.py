"""Two-sided immutable comparison requests and status-only job resources."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from backend.app.schemas import AttemptPublic, RunStatus
from backend.app.trace_schemas import TimestampEvidence
from packages.research_trace.comparison_contracts import (
    ALGORITHM_VERSION,
    TOLERANCE_VERSION,
    ComparisonLimits,
    ComparisonManifest,
)
from packages.research_trace.contracts import Contract, Sha256, StrategySpec, digest


class ComparisonSideCreate(Contract):
    evaluation_id: UUID
    execution_attempt_id: UUID
    result_manifest_sha256: Sha256
    # Required, even when null. Null requests verified historical absence, not
    # 'latest trace'; a registered off-mode trace keeps its actual SHA.
    trace_manifest_sha256: Sha256 | None
    source_kind: Literal["strategy_version", "candidate"]
    source_id: UUID
    code_sha256: Sha256
    spec_revision: int | None = Field(default=None, ge=1)
    spec_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def spec_pin(self):
        if (self.spec_revision is None) != (self.spec_sha256 is None):
            raise ValueError("Description revision and hash must be supplied together")
        return self


class ComparisonOptions(Contract):
    max_runtime_seconds: int = Field(default=300, ge=1, le=3600, strict=True)


class ComparisonCreate(Contract):
    left: ComparisonSideCreate
    right: ComparisonSideCreate
    options: ComparisonOptions = Field(default_factory=ComparisonOptions)

    @property
    def sha256(self):
        return digest(self)


class ComparisonConfig(Contract):
    schema_version: Literal["tradegrad.comparison.v1"] = "tradegrad.comparison.v1"
    execution_mode: Literal["comparison"] = "comparison"
    comparison_id: UUID
    input_sha256: Sha256
    engine_input_sha256: Sha256
    request: ComparisonCreate
    algorithm_version: Literal["tradegrad.observed-comparison.v1"] = ALGORITHM_VERSION
    tolerance_version: Literal["tradegrad.comparison-tolerances.v1"] = TOLERANCE_VERSION
    limits: ComparisonLimits = Field(default_factory=ComparisonLimits)

    @property
    def sha256(self):
        return digest(self)


class ComparisonPublic(Contract):
    id: UUID
    run_id: UUID
    status: RunStatus
    generation: int
    execution_mode: Literal["comparison"] = "comparison"
    input_sha256: Sha256
    engine_input_sha256: Sha256
    request: ComparisonCreate
    active_attempt_id: UUID | None
    next_event_seq: int
    artifact_ready: bool
    manifest_artifact_id: UUID | None
    manifest_sha256: Sha256 | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime


class ComparisonDetail(ComparisonPublic):
    attempts: list[AttemptPublic]


class ComparisonSource(Contract):
    source_strategy_version_id: UUID
    source_kind: Literal["strategy_version", "candidate"]
    source_id: UUID
    code_sha256: Sha256
    code: str
    spec: StrategySpec | None
    spec_sha256: Sha256 | None
    spec_missing_reason: str | None


class ComparisonResult(Contract):
    comparison_id: UUID
    input_sha256: Sha256
    manifest_artifact_id: UUID
    manifest_sha256: Sha256
    manifest: ComparisonManifest
    left_source: ComparisonSource
    right_source: ComparisonSource
    left_timestamp_evidence: TimestampEvidence | None = None
    right_timestamp_evidence: TimestampEvidence | None = None


from packages.research_trace.comparison_contracts import ComparisonPage


class ComparisonSectionPage(ComparisonPage):
    comparison_id: UUID
    input_sha256: Sha256
    manifest_sha256: Sha256
