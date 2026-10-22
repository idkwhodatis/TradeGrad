"""Bounded M3 API provenance contracts; holdout execution is intentionally absent."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from backend.app.schemas import AttemptPublic, PublicModel, RunPublic
from packages.tradegrad_adapter.config import RunConfig


class ResearchRunCreate(RunConfig):
    execution_mode: Literal["optimization"] = "optimization"


class ResearchRunPublic(RunPublic):
    research_image_digest: str | None = None
    latest_checkpoint_id: UUID | None = None
    resume_checkpoint_id: UUID | None = None


class ResearchRunDetail(ResearchRunPublic):
    attempts: list[AttemptPublic]


class CandidatePublic(PublicModel):
    id: UUID
    run_id: UUID
    attempt_id: UUID
    generation: int
    candidate_key: str
    parent_id: UUID | None
    kind: Literal["seed", "generated", "repair"]
    iteration: int
    repair_index: int
    code_sha256: str
    created_at: datetime


class CandidateDetail(CandidatePublic):
    code: str


class EvaluationPublic(PublicModel):
    id: UUID
    run_id: UUID
    attempt_id: UUID
    generation: int
    candidate_id: UUID | None
    candidate_key: str
    code_sha256: str
    split: Literal["train", "validation"]
    window: dict[str, Any]
    window_sha256: str
    image_digest: str
    sandbox_id: str
    sandbox_stopped: bool
    status: str
    payload_sha256: str | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class EvaluationDetail(EvaluationPublic):
    payload: dict[str, Any] | None


class ModelCallPublic(PublicModel):
    id: UUID
    run_id: UUID
    attempt_id: UUID
    generation: int
    logical_call_id: str
    provider: str
    model: str
    prompt_version: str
    status: str
    adopted: bool
    request_sha256: str
    response_sha256: str | None
    response_artifact: dict[str, Any] | None
    usage: dict[str, Any]
    latency_seconds: float = Field(ge=0, allow_inf_nan=False)
    error_category: str | None
    payload_sha256: str
    created_at: datetime


class ModelCallDetail(ModelCallPublic):
    payload: dict[str, Any]


class CheckpointPublic(PublicModel):
    id: UUID
    run_id: UUID
    attempt_id: UUID
    generation: int
    sequence: int
    schema_version: int
    manifest_sha256: str
    input_digest: str
    config_sha256: str
    next_stage: str
    iteration: int
    complete: bool
    created_at: datetime


class CheckpointDetail(CheckpointPublic):
    summary: dict[str, Any]
