from datetime import date, datetime
from typing import Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from packages.tradegrad_adapter.native_contract import NativeStrategyContract

Task = Literal["cn_time", "us_time", "cn_cross", "us_cross"]
RunStatus = Literal[
    "queued",
    "preparing",
    "running",
    "finalizing",
    "pause_requested",
    "paused",
    "cancel_requested",
    "cancelled",
    "failed",
    "interrupted",
    "succeeded",
]


class PublicModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserPublic(PublicModel):
    id: UUID
    email: EmailStr
    full_name: str | None
    is_active: bool
    is_superuser: bool


class Token(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class DatasetPublic(PublicModel):
    id: UUID
    name: str
    market: Literal["CN", "US"]
    start_date: date
    end_date: date = Field(description="Exclusive end date, [start_date, end_date)")
    source: str
    is_synthetic: bool
    content_sha256: str
    manifest: dict[str, Any]
    created_at: datetime


class StrategyPublic(PublicModel):
    id: UUID
    name: str
    task: Task
    mode: Literal["native_akquant"]
    manifest: NativeStrategyContract | None = None
    parameters: dict[str, bool | int | float | str] | None = None
    content_sha256: str
    parent_id: UUID | None
    created_at: datetime


class StrategyDetail(StrategyPublic):
    code: str


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: Task
    dataset_version_id: UUID
    strategy_version_id: UUID
    start_date: date
    end_date: date = Field(description="Exclusive end date")
    profile: Literal["corrected_explicit_config"] = "corrected_explicit_config"

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.start_date >= self.end_date:
            raise ValueError("start_date must precede exclusive end_date")
        return self


class AttemptPublic(PublicModel):
    id: UUID
    resumed_checkpoint_id: UUID | None = None
    generation: int
    status: str
    sandbox_stopped: bool
    exit_code: int | None
    error: str | None
    heartbeat_at: datetime
    started_at: datetime
    finished_at: datetime | None


class ResultPublic(BaseModel):
    metrics: dict[str, float]
    combined_score: float | None = None
    execution_mode: Literal["seed_evaluation"] = "seed_evaluation"
    is_synthetic: bool
    evaluation_mode: Literal["native_akquant"] = "native_akquant"
    result_trust: Literal["native_strategy_and_engine_share_process"] = (
        "native_strategy_and_engine_share_process"
    )
    image_digest: str | None = None


class OptimizationResultPublic(BaseModel):
    execution_mode: Literal["optimization"] = "optimization"
    stop_reason: str
    best_candidate_id: UUID | None = None
    best_candidate_key: str | None = None
    combined_score: float | None = Field(default=None, allow_inf_nan=False)
    metrics: dict[str, float] = Field(default_factory=dict)
    is_synthetic: bool
    evaluation_mode: Literal["native_akquant"] = "native_akquant"
    result_trust: Literal["native_strategy_and_engine_share_process"] = (
        "native_strategy_and_engine_share_process"
    )
    image_digest: str | None = None
    holdout_executed: Literal[False] = False


class RunPublic(PublicModel):
    id: UUID
    status: RunStatus
    generation: int
    config: dict[str, Any]
    dataset_version_id: UUID
    strategy_version_id: UUID
    active_attempt_id: UUID | None
    next_event_seq: int
    result: ResultPublic | OptimizationResultPublic | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    execution_mode: Literal["seed_evaluation", "optimization"] = "seed_evaluation"


class RunDetail(RunPublic):
    attempts: list[AttemptPublic]


class EventPublic(PublicModel):
    schema_version: int
    run_id: UUID
    attempt_id: UUID | None
    seq: int
    occurred_at: datetime
    type: str
    payload: dict[str, Any]


class ArtifactPublic(PublicModel):
    id: UUID
    run_id: UUID
    attempt_id: UUID
    name: str
    media_type: str
    sha256: str
    size_bytes: int
    created_at: datetime


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    data: list[T]
    count: int


class ErrorResponse(BaseModel):
    code: str
    message: str
    details: Any = None
    correlation_id: str


class HealthResponse(BaseModel):
    status: str
    database: bool
    queue_pending: int | None = None
    workers: dict[str, str] = {}
