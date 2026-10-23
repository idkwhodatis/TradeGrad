"""One immutable portable holdout execution, never research scheduling authority."""

import hashlib
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from packages.strategy_sdk import canonical_json
from packages.tradegrad_adapter.config import (
    DateWindow,
    FrozenModel,
    Sha256,
    VersionRef,
)
from packages.tradegrad_adapter.portable_config import PortableRunConfig


class PortableFinalEvaluationConfig(FrozenModel):
    schema_version: Literal["tradegrad.portable.final-evaluation.v1"] = (
        "tradegrad.portable.final-evaluation.v1"
    )
    evaluation_mode: Literal["portable"] = "portable"
    execution_mode: Literal["final_evaluation"] = "final_evaluation"
    selection_id: UUID
    split_version_id: UUID
    split_sha256: Sha256
    selected_strategy: VersionRef
    selected_manifest: VersionRef
    selected_parameters: VersionRef
    source_config: PortableRunConfig
    source_config_sha256: Sha256
    holdout: DateWindow
    environment_digest: Sha256
    max_runtime_seconds: int = Field(ge=1, le=3600)

    @model_validator(mode="after")
    def exact_source(self):
        if self.source_config.execution_mode != "optimization":
            raise ValueError(
                "Final evaluation requires a frozen portable optimization source"
            )
        if self.source_config.sha256 != self.source_config_sha256:
            raise ValueError("Exact source portable RunConfig digest mismatch")
        if self.holdout != self.source_config.windows.holdout:
            raise ValueError("Holdout differs from the frozen source split")
        if self.environment_digest != self.source_config.environment_digest:
            raise ValueError("Final evaluation cannot change the pinned environment")
        if self.selected_manifest.id != f"{self.selected_strategy.id}/manifest":
            raise ValueError("Selected manifest must belong to frozen strategy version")
        if self.selected_parameters.id != f"{self.selected_strategy.id}/parameters":
            raise ValueError(
                "Selected parameters must belong to frozen strategy version"
            )
        if self.selected_parameters.sha256 != self.source_config.seed_parameters.sha256:
            raise ValueError("Final evaluation cannot change business parameters")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json(self)

    @property
    def sha256(self):
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    # Semantic properties are read-only views, not a research config or window API.
    @property
    def seed_parameters(self):
        return self.selected_parameters

    @property
    def seed_strategy(self):
        return self.source_config.seed_strategy

    @property
    def strategy_kind(self):
        return self.source_config.strategy_kind

    @property
    def market(self):
        return self.source_config.market

    @property
    def initial_state(self):
        return self.source_config.initial_state

    @property
    def engine_profile(self):
        return self.source_config.engine_profile
