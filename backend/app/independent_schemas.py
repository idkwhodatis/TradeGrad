"""Immutable diagnostic and manual-control requests and status resources."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from backend.app.experiment_schemas import ExposureSnapshot
from backend.app.schemas import AttemptPublic, RunStatus
from packages.research_trace.contracts import CaptureConfig, Contract, Sha256, digest
from packages.tradegrad_adapter.independent_config import (
    IndependentEvaluationConfig,
    IndependentSourcePin,
    PortableIndependentEvaluationConfig,
)


class DiagnosticReplayCreate(Contract):
    source: IndependentSourcePin
    research_capture: CaptureConfig = Field(
        default_factory=lambda: CaptureConfig(mode="research")
    )
    max_runtime_seconds: int = Field(default=300, ge=1, le=3600, strict=True)

    @property
    def sha256(self):
        return digest(self)


class ControlledArmCreate(Contract):
    planned_change: str | None = Field(default=None, max_length=4000)
    name: str = Field(min_length=1, max_length=120)
    code: str = Field(min_length=1, max_length=900000)
    parameters: dict[str, Any] | None = None


class ControlledExperimentCreate(DiagnosticReplayCreate):
    name: str = Field(min_length=1, max_length=255)
    hypothesis: str = Field(min_length=1, max_length=4000)
    planned_change: str = Field(min_length=1, max_length=4000)
    arms: tuple[ControlledArmCreate, ...] = Field(min_length=2, max_length=3)

    @model_validator(mode="after")
    def distinct_names(self):
        if len({arm.name for arm in self.arms}) != len(self.arms):
            raise ValueError("Control arm names must be distinct")
        return self


class IndependentCapability(Contract):
    source: IndependentSourcePin
    source_run_id: UUID
    original_config_sha256: Sha256
    original_config: dict[str, Any]
    evaluation_mode: Literal["native_akquant", "portable"]
    can_execute: bool
    can_control: bool
    control_unavailable_reasons: list[str]
    unavailable_reasons: list[str]
    parity_prerequisites: dict[str, Any]
    fixed_conditions: dict[str, Any]
    protocol_sha256: Sha256
    source_code: str
    source_parameters: dict[str, Any] | None
    exposure_snapshot: ExposureSnapshot


class IndependentResultPin(Contract):
    execution_config_sha256: Sha256
    evaluation_id: UUID
    execution_attempt_id: UUID
    result_manifest_sha256: Sha256
    trace_manifest_sha256: Sha256 | None
    source_kind: Literal["strategy_version"] = "strategy_version"
    source_id: UUID
    code_sha256: Sha256


class IndependentEvaluationPublic(Contract):
    request: DiagnosticReplayCreate
    id: UUID
    independent_id: UUID
    status: RunStatus
    generation: int
    purpose: Literal["diagnostic", "controlled"]
    request_sha256: Sha256
    config_sha256: Sha256
    config: IndependentEvaluationConfig | PortableIndependentEvaluationConfig
    source: IndependentSourcePin
    experiment_id: UUID
    split_version_id: UUID
    active_attempt_id: UUID | None
    next_event_seq: int
    evaluation_id: UUID | None
    result_ready: bool
    result_pin: IndependentResultPin | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class IndependentEvaluationDetail(IndependentEvaluationPublic):
    attempts: list[AttemptPublic]


class ControlledArmPublic(Contract):
    planned_change: str | None
    ordinal: int
    name: str
    strategy_version_id: UUID
    code_sha256: Sha256
    run_id: UUID
    status: RunStatus
    evaluation_id: UUID | None
    result_pin: IndependentResultPin | None
    changed_fields: list[str]
    interpretation: Literal[
        "baseline",
        "single_parameter_change_not_causal_proof",
        "not_individually_validated",
        "unchanged",
    ]
    parameters: dict[str, Any] | None
    error: str | None


class ControlledExperimentPublic(Contract):
    request: ControlledExperimentCreate
    id: UUID
    experiment_id: UUID
    split_version_id: UUID
    parent_experiment_id: UUID
    root_experiment_id: UUID
    source: IndependentSourcePin
    request_sha256: Sha256
    name: str
    hypothesis: str
    planned_change: str
    protocol: dict[str, Any]
    protocol_sha256: Sha256
    arms: list[ControlledArmPublic]
    exposure_snapshot: ExposureSnapshot
    created_at: datetime


class IndependentResultQuery(Contract):
    evaluation_id: UUID
    run_id: UUID
    purpose: Literal["diagnostic", "controlled"]
    artifact_sha256: Sha256
    evaluation_mode: Literal["native_akquant", "portable"]
    metrics: dict[str, Any]
    metric_details: list[dict[str, Any]]
    reward_status: Literal["not_applicable"] = "not_applicable"
    result_trust: str
    section: str
    columns: list[str]
    execution_attempt_id: UUID
    rows: list[Any]
    total: int
    offset: int
    limit: int
    warnings: list[str]
