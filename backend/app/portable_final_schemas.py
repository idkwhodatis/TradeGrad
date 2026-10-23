"""A final evaluation is not an optimizer run and cannot mutate its inputs."""

import hashlib
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.app.experiment_schemas import ExposureSnapshot
from backend.app.schemas import AttemptPublic, PublicModel, RunStatus
from packages.tradegrad_adapter.config import canonical_bytes
from packages.tradegrad_adapter.portable_final_config import (
    PortableFinalEvaluationConfig,
)


class PortableFinalEvaluationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection_id: UUID
    max_runtime_seconds: int = Field(default=300, ge=1, le=3600)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_bytes(self.model_dump(mode="json"))).hexdigest()


class PortableFinalEvaluationResult(PublicModel):
    execution_mode: Literal["final_evaluation"] = "final_evaluation"
    evaluation_id: UUID | None = None
    selection_id: UUID
    metrics: dict[str, float] = Field(default_factory=dict)
    combined_score: float | None = Field(default=None, allow_inf_nan=False)
    is_synthetic: bool
    evaluation_mode: Literal["portable"] = "portable"
    result_trust: Literal["isolated_policy_trusted_akquant_engine"] | None = None
    ledger_origin: Literal["trusted_engine"] | None = None
    runtime_certified: Literal[False] = False
    external_framework_equivalence: Literal["not_certified"] = "not_certified"
    objective_definition: Literal["portable_return_drawdown.v1"]
    policy_image_digest: str | None
    holdout_executed: bool
    image_digest: str | None
    exposure_snapshot: ExposureSnapshot


class PortableFinalEvaluationPublic(PublicModel):
    id: UUID
    status: RunStatus
    generation: int
    execution_mode: Literal["final_evaluation"] = "final_evaluation"
    config: PortableFinalEvaluationConfig
    experiment_id: UUID
    split_version_id: UUID
    final_selection_id: UUID
    dataset_version_id: UUID
    strategy_version_id: UUID
    active_attempt_id: UUID | None
    next_event_seq: int
    research_image_digest: str
    portable_policy_image_digest: str
    result: PortableFinalEvaluationResult | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class PortableFinalEvaluationDetail(PortableFinalEvaluationPublic):
    attempts: list[AttemptPublic]


class PortableFinalEvaluationPreflight(PublicModel):
    eligible: bool
    selection_id: UUID
    config: PortableFinalEvaluationConfig
    exposure_snapshot: ExposureSnapshot
    warnings: list[str]
    quota_remaining: int = Field(ge=0)


class PortableFinalEvaluationEvidence(PublicModel):
    id: UUID
    run_id: UUID
    attempt_id: UUID
    generation: int
    candidate_id: UUID | None
    candidate_key: str
    code_sha256: str
    split: Literal["holdout"]
    window: dict[str, Any]
    window_sha256: str
    image_digest: str
    sandbox_id: str
    sandbox_stopped: bool
    status: str
    payload: dict[str, Any] | None
    payload_sha256: str | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None
