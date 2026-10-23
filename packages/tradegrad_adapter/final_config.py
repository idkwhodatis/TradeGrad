"""Strict, distinct final-evaluation inputs; old RunConfig bytes are unchanged."""

import hashlib
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from packages.tradegrad_adapter.config import (
    DateWindow,
    FrozenModel,
    RunConfig,
    Sha256,
    VersionRef,
    canonical_bytes,
)


class FinalEvaluationConfig(FrozenModel):
    schema_version: Literal["tradegrad.final-evaluation.v1"] = (
        "tradegrad.final-evaluation.v1"
    )
    execution_mode: Literal["final_evaluation"] = "final_evaluation"
    selection_id: UUID
    split_version_id: UUID
    split_sha256: Sha256
    selected_strategy: VersionRef
    source_config: RunConfig
    source_config_sha256: Sha256
    holdout: DateWindow
    environment_digest: Sha256
    max_runtime_seconds: int = Field(ge=1, le=3600)

    @model_validator(mode="after")
    def exact_source(self):
        if self.source_config.execution_mode != "optimization":
            raise ValueError("Final evaluation requires a frozen optimization source")
        if self.source_config.sha256 != self.source_config_sha256:
            raise ValueError("Exact source RunConfig digest mismatch")
        if self.holdout != self.source_config.windows.holdout:
            raise ValueError("Holdout differs from the frozen source split")
        if self.environment_digest != self.source_config.environment_digest:
            raise ValueError("Final evaluation cannot change the pinned environment")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.model_dump(mode="json"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()
