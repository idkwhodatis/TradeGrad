"""Owner-scoped, source-bound description and expandable lineage API contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.research_trace.contracts import Sha256, StrategySpec
from packages.tradegrad_adapter.native_contract import NativeStrategyContract

ResourceKind = Literal["strategy_version", "candidate"]


class WorkbenchSpec(BaseModel):
    resource_kind: ResourceKind
    resource_id: UUID
    strategy_version_id: UUID
    candidate_id: UUID | None
    code_sha256: Sha256
    spec_sha256: Sha256
    revision: int
    created_at: datetime
    origin: Literal["static", "human"]
    change_note: str | None
    spec: StrategySpec


class RuleProseEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def visible(self):
        if not self.title.strip() or not self.description.strip():
            raise ValueError("Rule prose must contain visible text")
        return self


class SpecRevisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code_sha256: Sha256
    base_revision: int = Field(ge=1, le=1_000_000)
    base_spec_sha256: Sha256
    rules: list[RuleProseEdit] = Field(min_length=1, max_length=128)
    change_note: str | None = Field(default=None, max_length=2000)


class CandidateBookmarkSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bookmarked: bool


class CandidateBookmark(BaseModel):
    candidate_id: UUID
    code_sha256: Sha256
    bookmarked: bool


class WorkbenchSourceAssociation(BaseModel):
    candidate_id: UUID | None
    candidate_basis: Literal["registered_fk", "same_run_key_hash", "unavailable"]
    strategy_version_id: UUID | None
    strategy_basis: Literal["saved_seed", "selected_final", "unavailable"]
    missing_reasons: list[str]


class WorkbenchEvaluation(BaseModel):
    source_association: WorkbenchSourceAssociation
    run_kind: Literal[
        "native_research",
        "native_final",
        "portable_research",
        "portable_final",
        "legacy",
    ]
    run_schema_version: str | None
    final_test_id: UUID | None
    evaluation_id: UUID
    run_id: UUID
    strategy_version_id: UUID
    candidate_id: UUID | None
    execution_attempt_id: UUID
    code_sha256: Sha256
    status: str
    split: str
    window: dict
    dataset_version_id: UUID
    universe_version_id: UUID | None
    result_manifest_sha256: Sha256 | None
    trace_manifest_sha256: Sha256 | None
    evidence_status: Literal["registered", "unavailable"]
    missing_reasons: list[str]
    error: str | None
    created_at: datetime


class LineageNode(BaseModel):
    resource_kind: ResourceKind
    id: UUID
    strategy_version_id: UUID
    code_sha256: Sha256
    name: str
    kind: Literal["strategy", "derived", "seed", "generated", "repair"]
    mode: str
    created_at: datetime
    run_id: UUID | None
    candidate_key: str | None
    parent_id: UUID | None
    bookmarked: bool
    is_recorded_best: bool | None
    selection_missing_reason: str | None
    final_selection_ids: list[UUID]
    final_selection_count: int
    final_selections_truncated: bool
    aggregate_status: Literal["not_loaded"]
    aggregate_missing_reason: str
    evaluation_count: int
    evaluations: list[WorkbenchEvaluation]
    evaluations_truncated: bool
    child_count: int


class LineageReference(BaseModel):
    candidate_id: UUID
    candidate_key: str
    code_sha256: Sha256
    relationship: Literal["algorithm_reference"] = "algorithm_reference"
    checkpoint_id: UUID | None = None
    checkpoint_sha256: Sha256 | None = None


class LineageContext(BaseModel):
    status: Literal["available", "not_collected", "unavailable"]
    checkpoint_id: UUID | None = None
    checkpoint_sha256: Sha256 | None = None
    feedback: str | None = None
    revision_mode: str | None = None
    generation_status: str | None = None
    feedback_sha256: Sha256 | None = None
    feedback_truncated: bool = False
    model_call_ids: list[UUID] = Field(default_factory=list)
    missing_reasons: list[str] = Field(default_factory=list)


class WorkbenchLineage(BaseModel):
    focus: LineageNode
    parent: LineageNode | None
    seed_strategy: LineageNode | None
    children: list[LineageNode]
    child_count: int
    offset: int
    limit: int
    reference_candidates: list[LineageReference]
    context: LineageContext
    missing_reasons: list[str]


class ResearchTemplate(BaseModel):
    template_id: Literal["daily_timing", "cross_sectional"]
    name: str
    task_suffix: Literal["time", "cross"]
    code: str
    code_sha256: Sha256
    manifest: NativeStrategyContract
    spec: StrategySpec
