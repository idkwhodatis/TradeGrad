"""Distinct owner-facing portable API contracts; never native DTO aliases."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.app.schemas import AttemptPublic, PublicModel, RunStatus
from backend.app.strategy_schemas import StrategyCreate
from packages.strategy_sdk.contracts import StrategyManifest
from packages.tradegrad_adapter.config import VersionRef, WindowSet
from packages.tradegrad_adapter.portable_config import PortableRunConfig


class PortableRunCreate(PortableRunConfig):
    pass


class PortableResultPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    execution_mode: Literal["optimization", "seed_evaluation"]
    stop_reason: str
    best_candidate_id: UUID | None = None
    best_candidate_key: str | None = None
    combined_score: float | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    is_synthetic: bool
    evaluation_mode: Literal["portable"] = "portable"
    result_trust: Literal["isolated_policy_trusted_akquant_engine"] | None = None
    ledger_origin: Literal["trusted_engine"] | None = None
    runtime_certified: Literal[False] = False
    external_framework_equivalence: Literal["not_certified"] = "not_certified"
    image_digest: str | None = None
    policy_image_digest: str | None = None
    objective_definition: Literal["portable_return_drawdown.v1"]
    holdout_executed: Literal[False] = False


class PortableRunPublic(PublicModel):
    id: UUID
    status: RunStatus
    generation: int
    config: PortableRunConfig
    dataset_version_id: UUID
    strategy_version_id: UUID
    experiment_id: UUID | None = None
    split_version_id: UUID | None = None
    active_attempt_id: UUID | None
    next_event_seq: int
    research_image_digest: str | None = None
    portable_policy_image_digest: str | None = None
    latest_checkpoint_id: UUID | None = None
    resume_checkpoint_id: UUID | None = None
    result: PortableResultPublic | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    execution_mode: Literal["seed_evaluation", "optimization"]


class PortableRunDetail(PortableRunPublic):
    attempts: list[AttemptPublic]


class PortableStrategyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="Portable reference policy", min_length=1, max_length=255)
    strategy_kind: Literal["portfolio", "event"]
    dataset: VersionRef
    universe: VersionRef
    initial_cash: float = Field(default=1_000_000, gt=0, le=1e12)


class PortableSourceCreate(StrategyCreate):
    """Identity hashes are server-assigned; all other manifest claims are frozen."""

    manifest: StrategyManifest
    parameters: dict[str, Any]


class PortableStrategyPublic(PublicModel):
    id: UUID
    name: str
    task: Literal["cn_time", "us_time", "cn_cross", "us_cross"]
    mode: Literal["portable"]
    parent_id: UUID | None = None
    content_sha256: str
    manifest: dict[str, Any]
    parameters: dict[str, Any]
    created_at: datetime


class PortableStrategyDetail(PortableStrategyPublic):
    code: str


class PortableDefaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset: VersionRef
    seed_strategy: VersionRef
    universe: VersionRef
    windows: WindowSet
    execution_mode: Literal["optimization", "seed_evaluation"] = "optimization"


class PortableTemplatePublic(BaseModel):
    strategy_kind: Literal["portfolio", "event"]
    template_version: Literal["portable_business_rules.v1"]
    source: str
    code_sha256: str
    editable_region: Literal["business_rules_only"] = "business_rules_only"
    conformance: Literal["interface_only"] = "interface_only"
    runtime_certified: Literal[False] = False
    external_framework_equivalence: Literal["not_certified"] = "not_certified"


class PortablePreflightPublic(BaseModel):
    valid: Literal[True] = True
    config_sha256: str
    evaluation_windows: list[dict[str, str]]
    is_synthetic: bool
    data_evidence: Literal[
        "synthetic_by_construction", "user_declaration_unverified", "not_assessed"
    ] = "not_assessed"
    warnings: list[str]
    holdout_execution: Literal[False] = False
    runtime_certified: Literal[False] = False


class PortableResultQuery(BaseModel):
    run_id: UUID
    image_digest: str
    policy_image_digest: str
    environment_digest: str
    is_synthetic: bool
    data_evidence: Literal[
        "synthetic_by_construction", "user_declaration_unverified", "not_assessed"
    ] = "not_assessed"
    currency: Literal["USD", "CNY"]
    evaluation_id: UUID
    schema_version: Literal["tradegrad.portable-result.v1"]
    evaluation_mode: Literal["portable"] = "portable"
    result_trust: Literal["isolated_policy_trusted_akquant_engine"]
    ledger_origin: Literal["trusted_engine"]
    runtime_certified: Literal[False] = False
    external_framework_equivalence: Literal["not_certified"] = "not_certified"
    artifact_sha256: str
    identity: dict[str, str]
    window: dict[str, Any]
    summary: dict[str, Any]
    combined_score: float
    benchmark: dict[str, Any] | None = None
    section: Literal[
        "account_series",
        "positions",
        "orders",
        "fills",
        "closed_trades",
        "native_metrics",
        "decision_trace",
        "logical_orders",
    ]
    rows: list[dict[str, Any]]
    row_indices: list[int] | None = None
    total_rows: int | None = None
    sortable_columns: list[str] | None = None
    sort_by: str | None = None
    sort_direction: Literal["asc", "desc"] = "asc"
    side: Literal["buy", "sell"] | None = None
    count: int
    offset: int
    limit: int


class PortableEvaluationPublic(PublicModel):
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


class PortableEvaluationDetail(PortableEvaluationPublic):
    payload: dict[str, Any] | None
