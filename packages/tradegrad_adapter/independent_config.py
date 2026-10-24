"""One frozen leaf execution, not an optimizer configuration or checkpoint resume.

The source config is preserved byte-semantically. Only explicitly selected code,
business parameter values and capture differ; original physical source pins are
kept separately so replaying an independent leaf never nests configurations.
"""

from __future__ import annotations

import hashlib
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from packages.research_trace.contracts import CaptureConfig
from packages.tradegrad_adapter.config import (
    DateWindow,
    FrozenModel,
    RunConfig,
    Sha256,
    VersionRef,
    canonical_bytes,
)
from packages.tradegrad_adapter.native_contract import NativeStrategyContract
from packages.tradegrad_adapter.portable_config import PortableRunConfig

NATIVE_SCHEMA = "tradegrad.independent-evaluation.v1"
PORTABLE_SCHEMA = "tradegrad.portable.independent-evaluation.v1"
SCHEMAS = (NATIVE_SCHEMA, PORTABLE_SCHEMA)


class IndependentSourcePin(FrozenModel):
    evaluation_id: UUID
    execution_attempt_id: UUID
    result_manifest_sha256: Sha256
    trace_manifest_sha256: Sha256 | None
    source_kind: Literal["strategy_version", "candidate"]
    source_id: UUID
    code_sha256: Sha256
    spec_revision: int | None = Field(default=None, ge=1)
    spec_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def paired_spec(self):
        if (self.spec_revision is None) != (self.spec_sha256 is None):
            raise ValueError("Description revision and hash must be paired")
        return self


class IndependentBase(FrozenModel):
    execution_mode: Literal["independent_evaluation"] = "independent_evaluation"
    independent_id: UUID
    purpose: Literal["diagnostic", "controlled"]
    source: IndependentSourcePin
    source_run_id: UUID
    original_config_sha256: Sha256
    source_config_sha256: Sha256
    selected_strategy: VersionRef
    split: Literal["train", "validation", "holdout"]
    reconstruction_window: DateWindow
    state_policy: Literal["original_start_fresh_engine_with_original_warmup.v1"] = (
        "original_start_fresh_engine_with_original_warmup.v1"
    )
    research_capture: CaptureConfig = Field(
        default_factory=lambda: CaptureConfig(mode="research")
    )
    environment_digest: Sha256
    research_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    runtime_observation_sha256: Sha256 | None = None
    rng_restore: dict | None = None
    protocol_sha256: Sha256
    max_runtime_seconds: int = Field(default=300, ge=1, le=3600)

    @model_validator(mode="after")
    def frozen_conditions(self):
        if self.source_config.sha256 != self.source_config_sha256:
            raise ValueError("Flat frozen source config hash differs")
        if self.environment_digest != self.source_config.environment_digest:
            raise ValueError("Independent execution cannot change environment")
        original = self.source_config.windows.get(self.split)
        if not (
            original.start
            <= self.reconstruction_window.start
            < self.reconstruction_window.end
            <= original.end
        ):
            raise ValueError("Reconstruction window is outside original split")
        if self.purpose == "controlled" and self.split == "holdout":
            raise ValueError("Interactive controls cannot execute final holdout")
        if (
            self.purpose == "diagnostic"
            and self.selected_strategy.sha256 != self.source.code_sha256
        ):
            raise ValueError("A diagnostic cannot instrument or change original code")
        if self.rng_restore is not None:
            from packages.tradegrad_adapter.runtime_observation import (
                validate_rng_state,
            )

            validate_rng_state(
                self.rng_restore, include_numpy=self.evaluation_mode == "native_akquant"
            )
            if self.runtime_observation_sha256 is None:
                raise ValueError(
                    "RNG restoration requires immutable original observation"
                )
            if len(canonical_bytes(self.rng_restore)) > 128 * 1024:
                raise ValueError("RNG restoration exceeds bounded plain JSON")
        return self

    def canonical_bytes(self):
        return canonical_bytes(self.model_dump(mode="json"))

    @property
    def sha256(self):
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @property
    def window(self):
        return {
            "split": self.split,
            **self.reconstruction_window.model_dump(mode="json"),
        }


class IndependentEvaluationConfig(IndependentBase):
    schema_version: Literal["tradegrad.independent-evaluation.v1"] = NATIVE_SCHEMA
    evaluation_mode: Literal["native_akquant"] = "native_akquant"
    source_config: RunConfig
    selected_contract: NativeStrategyContract | None = None

    @model_validator(mode="after")
    def declarations_fixed(self):
        source = self.source_config.strategy_contract
        if contract_without_values(source) != contract_without_values(
            self.selected_contract
        ):
            raise ValueError(
                "Independent arms cannot change declarations, lookback or state contract"
            )
        if self.purpose == "diagnostic" and self.selected_contract != source:
            raise ValueError(
                "Diagnostic business parameters must match original execution"
            )
        return self

    def effective_source(self):
        raw = self.source_config.model_dump(mode="json")
        raw["strategy_contract"] = (
            self.selected_contract.model_dump(mode="json")
            if self.selected_contract
            else None
        )
        raw["research_capture"] = self.research_capture.model_dump(mode="json")
        return RunConfig.model_validate(raw)


class PortableIndependentEvaluationConfig(IndependentBase):
    schema_version: Literal["tradegrad.portable.independent-evaluation.v1"] = (
        PORTABLE_SCHEMA
    )
    evaluation_mode: Literal["portable"] = "portable"
    source_config: PortableRunConfig
    selected_manifest: VersionRef
    selected_parameters: VersionRef
    portable_policy_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def selected_refs(self):
        if (
            self.selected_manifest.id != f"{self.selected_strategy.id}/manifest"
            or self.selected_parameters.id != f"{self.selected_strategy.id}/parameters"
        ):
            raise ValueError(
                "Selected portable metadata must belong to selected strategy"
            )
        if (
            self.purpose == "diagnostic"
            and self.selected_parameters.sha256
            != self.source_config.seed_parameters.sha256
        ):
            raise ValueError("Diagnostic parameters must match original execution")
        return self

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

    def effective_source(self):
        raw = self.source_config.model_dump(mode="json")
        raw.update(
            seed_strategy={
                "id": self.source_config.seed_strategy.id,
                "sha256": self.selected_strategy.sha256,
            },
            seed_manifest=self.selected_manifest.model_dump(mode="json"),
            seed_parameters=self.selected_parameters.model_dump(mode="json"),
            research_capture=self.research_capture.model_dump(mode="json"),
        )
        return PortableRunConfig.model_validate(raw)


def contract_without_values(contract):
    if contract is None:
        return None
    raw = (
        contract.model_dump(mode="json")
        if hasattr(contract, "model_dump")
        else dict(contract)
    )
    raw["parameters"] = [
        {key: value for key, value in item.items() if key != "value"}
        for item in raw["parameters"]
    ]
    return raw


def parse_independent_config(value):
    model = {
        NATIVE_SCHEMA: IndependentEvaluationConfig,
        PORTABLE_SCHEMA: PortableIndependentEvaluationConfig,
    }.get(value.get("schema_version"))
    if model is None:
        raise ValueError("Not a distinct independent evaluation config")
    return model.model_validate(value)
