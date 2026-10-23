"""Immutable selection contracts independent of legacy RunConfig serialization."""

import hashlib
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.app.schemas import PublicModel
from packages.tradegrad_adapter.config import (
    FrozenModel,
    VersionRef,
    WindowSet,
    canonical_bytes,
)


class ExperimentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    parent_experiment_id: UUID | None = None


class ExperimentPublic(PublicModel):
    id: UUID
    name: str
    root_experiment_id: UUID | None
    parent_experiment_id: UUID | None
    created_at: datetime


class SplitDefinition(FrozenModel):
    schema_version: Literal["tradegrad.split.v1"] = "tradegrad.split.v1"
    dataset: VersionRef
    universe: VersionRef
    windows: WindowSet

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.model_dump(mode="json"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class SplitVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    experiment_id: UUID
    definition: SplitDefinition


class SplitVersionPublic(PublicModel):
    id: UUID
    experiment_id: UUID
    dataset_version_id: UUID
    universe_version_id: UUID
    content_sha256: str
    definition: SplitDefinition
    created_at: datetime


class FinalSelectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    split_version_id: UUID
    source_run_id: UUID
    source_candidate_id: UUID
    source_checkpoint_id: UUID

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_bytes(self.model_dump(mode="json"))).hexdigest()


class ExposureSnapshot(FrozenModel):
    schema_version: Literal["tradegrad.exposure.v1"] = "tradegrad.exposure.v1"
    classification: Literal[
        "previously_exposed", "no_tracked_overlap", "unknown_identity"
    ]
    matching_exposure_ids: list[UUID]
    matching_purposes: list[str]
    matching_count: int = Field(ge=0)
    evidence_truncated: bool = False
    identities: list[dict[str, Any]]
    as_of: datetime
    tracking_coverage: Literal[
        "historical tracking incomplete/external use unknown"
    ] = "historical tracking incomplete/external use unknown"
    independent_unseen_holdout_established: Literal[False] = False


class FinalSelectionPublic(PublicModel):
    id: UUID
    experiment_id: UUID
    split_version_id: UUID
    source_run_id: UUID
    source_candidate_id: UUID
    source_checkpoint_id: UUID
    strategy_version_id: UUID
    request_sha256: str
    source_config_sha256: str
    source_code_sha256: str
    source_checkpoint_sha256: str
    selection_evidence: dict[str, Any]
    exposure_snapshot: ExposureSnapshot
    created_at: datetime


class DataUsageExposurePublic(PublicModel):
    id: UUID
    experiment_id: UUID | None
    split_version_id: UUID | None
    run_id: UUID | None
    dataset_version_id: UUID | None
    source_key: str
    identities: list[dict[str, Any]]
    start_date: date
    end_date: date
    purpose: str
    provenance: dict[str, Any]
    created_at: datetime
