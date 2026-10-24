"""Data-only immutable I4 comparison contracts; no engine or service imports.

An equality statement is restricted to explicitly named, actually observed
sections. It is never diagnostic-parity certification or a causal claim.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from .contracts import (
    CaptureConfig,
    Contract,
    DateRange,
    EvaluationBinding,
    Sha256,
    SourceRef,
    StrategySpec,
    TraceManifest,
)

LEGACY_ALGORITHM_VERSION = "tradegrad.observed-comparison.v1"
ALGORITHM_VERSION = "tradegrad.observed-comparison.v2"
ComparisonAlgorithmVersion = Literal[
    "tradegrad.observed-comparison.v1", "tradegrad.observed-comparison.v2"
]
# Compatibility is read-only: historical identities are never recomputed with
# new semantics or rewritten into the current algorithm's cache namespace.
READABLE_ALGORITHM_VERSIONS = frozenset({LEGACY_ALGORITHM_VERSION, ALGORITHM_VERSION})
TOLERANCE_VERSION = "tradegrad.comparison-tolerances.v1"
# Only these named numerical fields have a tolerance. Integers, booleans,
# identities, timestamps, missing values and every other field compare exactly.
FIELD_TOLERANCES = {
    "score": {"absolute": 1e-12, "relative": 1e-10},
    "weight": {"absolute": 1e-12, "relative": 1e-10},
    "target_weight": {"absolute": 1e-12, "relative": 1e-10},
    "cash": {"absolute": 1e-7, "relative": 1e-10},
    "equity": {"absolute": 1e-7, "relative": 1e-10},
    "price": {"absolute": 1e-7, "relative": 1e-10},
    "close": {"absolute": 1e-7, "relative": 1e-10},
    "entry_price": {"absolute": 1e-7, "relative": 1e-10},
    "limit_price": {"absolute": 1e-7, "relative": 1e-10},
    "commission": {"absolute": 1e-7, "relative": 1e-10},
    "market_value": {"absolute": 1e-7, "relative": 1e-10},
    "unrealized_pnl": {"absolute": 1e-7, "relative": 1e-10},
    "pnl": {"absolute": 1e-7, "relative": 1e-10},
    "net_pnl": {"absolute": 1e-7, "relative": 1e-10},
}
SECTIONS = (
    "events",
    "candidates",
    "features",
    "predicates",
    "targets",
    "orders",
    "fills",
    "positions",
    "account",
    "turnover",
    "holding_duration",
)
COMMON_CONFIG_FIELDS = (
    "data",
    "universe",
    "window",
    "warmup",
    "initial_state",
    "state_policy",
    "fees",
    "fill",
    "market",
    "RNG",
    "engine",
)


class ComparisonError(ValueError):
    """Categorical public error without filesystem or SQL diagnostics."""

    def __init__(
        self,
        code="comparison_integrity",
        message="Comparison evidence failed integrity verification",
    ):
        self.code, self.message = code, message
        super().__init__(message)


def require_current_algorithm(algorithm_version):
    """Fail closed before executing a queued/retried historical computation."""
    if algorithm_version != ALGORITHM_VERSION:
        raise ComparisonError(
            "comparison_algorithm_unsupported",
            "This historical comparison algorithm is read-only; submit a new comparison",
        )


class ComparisonLimits(Contract):
    max_input_bytes: int = Field(default=4 * 1024**3, ge=1024, le=4 * 1024**3)
    max_input_rows: int = Field(default=40_000_000, ge=1, le=40_000_000)
    max_output_bytes: int = Field(default=2 * 1024**3, ge=1024, le=2 * 1024**3)
    max_output_rows: int = Field(default=40_000_000, ge=1, le=40_000_000)
    max_files: int = Field(default=4096, ge=1, le=4096)
    max_events: int = Field(default=100_000, ge=1, le=100_000)
    max_graph_edges: int = Field(default=100_000, ge=1, le=100_000)
    max_graph_bytes: int = Field(default=64 * 1024**2, ge=1024, le=64 * 1024**2)
    max_event_index_bytes: int = Field(default=64 * 1024**2, ge=1024, le=64 * 1024**2)
    max_event_rows: int = Field(default=100_000, ge=1, le=100_000)
    max_event_bytes: int = Field(default=64 * 1024**2, ge=1024, le=64 * 1024**2)
    max_record_bytes: int = Field(default=32 * 1024**2, ge=1024, le=32 * 1024**2)
    max_manifest_bytes: int = Field(default=4 * 1024**2, ge=1024, le=4 * 1024**2)
    max_bundle_bytes: int = Field(default=8 * 1024**2, ge=1024, le=8 * 1024**2)
    block_rows: int = Field(default=8192, ge=1, le=16384)
    block_bytes: int = Field(default=8 * 1024**2, ge=1024, le=8 * 1024**2)
    duckdb_memory_bytes: Literal[268435456] = 268435456
    duckdb_threads: Literal[1] = 1
    spill_bytes: Literal[0] = 0
    tolerant_multiset_max_size: Literal[64] = 64
    semantic_matching_nodes: Literal[65536] = 65536
    semantic_matching_bytes: Literal[67108864] = 67108864


DEFAULT_LIMITS = ComparisonLimits()


class EventNamespace(Contract):
    """Backend-derived evidence, not a caller-supplied 'verified' boolean."""

    dataset_version_id: UUID
    dataset_sha256: Sha256
    universe_version_id: UUID | None
    universe_sha256: Sha256 | None
    frozen_universe_sha256: Sha256 | None = None
    instrument_namespace: Literal["native_symbol", "portable_listing_id"]
    data_event_namespace: Literal["frozen_source_event.v1"] = "frozen_source_event.v1"

    @model_validator(mode="after")
    def paired(self):
        if (self.universe_version_id is None) != (self.universe_sha256 is None):
            raise ValueError("Universe version and hash must be supplied together")
        if self.universe_version_id is None and self.frozen_universe_sha256 is None:
            raise ValueError(
                "A missing universe reference requires a verified frozen-universe digest"
            )
        return self


@dataclass(frozen=True)
class ComparisonInput:
    """Already owner/source/result-admitted input; paths are server-owned only.

    The engine checks internal checksums and snapshots mutable JSON before work.
    Ownership, exposure, frozen-universe derivation and ResultBundle admission
    remain the business service's responsibility.
    """

    binding: EvaluationBinding
    source_kind: Literal["strategy_version", "candidate"]
    source_id: UUID
    source: str
    window: DateRange
    spec: StrategySpec | None
    spec_sha256: str | None
    trace_manifest: TraceManifest | None
    trace_manifest_sha256: str | None
    trace_paths: Mapping[str, Path]
    common_config: Mapping[str, Any]
    economic_bundle: Mapping[str, Any]
    namespace: EventNamespace | None


class ComparisonPublication(Contract):
    comparison_id: UUID
    run_id: UUID
    execution_attempt_id: UUID


class ComparisonSidePins(Contract):
    binding: EvaluationBinding
    source_kind: Literal["strategy_version", "candidate"]
    source_id: UUID
    window: DateRange
    spec_revision: int | None
    spec_sha256: Sha256 | None
    trace_manifest_sha256: Sha256 | None
    trace_state: Literal["registered", "registered_off", "historical_absence"]
    trace_coverage: str | None
    capture: CaptureConfig | None
    trace_missing_reasons: list[str]
    common_config_sha256: Sha256
    economic_bundle_sha256: Sha256
    namespace: EventNamespace | None


class ConfigurationDifference(Contract):
    field: str
    left_present: bool
    right_present: bool
    left: Any = None
    right: Any = None
    status: Literal["equal", "different", "unknown"]


class MechanismChange(Contract):
    category: Literal["input", "filter", "scoring", "portfolio", "execution"]
    rule_id: str | None
    status: Literal["equal", "changed", "left_only", "right_only", "unmapped"]
    mapping: Literal["controlled", "human_confirmed", "inferred", "unmapped"]
    left_source_ref: SourceRef | None
    right_source_ref: SourceRef | None
    left: dict[str, Any] | None
    right: dict[str, Any] | None
    reason: str | None = None


Availability = Literal["available", "partial", "not_collected", "unsupported"]
EvidenceState = Literal[
    "observed", "observed_absence", "incomplete_coverage", "not_collected", "unpaired"
]


class ComparisonSectionSummary(Contract):
    left_availability: Availability
    right_availability: Availability
    left_reason: str | None = None
    right_reason: str | None = None
    observed_equality: Literal["equal", "different", "unknown"] = "unknown"
    rows: int = 0
    equal: int = 0
    changed: int = 0
    left_only: int = 0
    right_only: int = 0
    ambiguous: int = 0
    unavailable: int = 0
    definition: str
    left_scope: dict[str, Any] = Field(default_factory=dict)
    right_scope: dict[str, Any] = Field(default_factory=dict)
    equality_scope: Literal["named_observations_only"] = "named_observations_only"


class ComparisonAssociation(Contract):
    side: Literal["left", "right"]
    event_pair_id: str
    decision_id: str
    stage_id: str
    logical_order_id: str
    instrument_id: str
    link_ordinal: int = Field(ge=0)
    source_event_paired: bool
    child_order_ids: list[str]
    fill_ids: list[str]


class ComparisonRecord(Contract):
    # Any-valued observations must never silently serialize nonfinite evidence
    # as null. Canonical storage rejects these constants instead.
    model_config = ConfigDict(**Contract.model_config, ser_json_inf_nan="constants")

    row_id: str
    section: str
    event_pair_id: str | None = None
    session_date: str | None = None
    instrument_id: str | None = None
    stage_id: str | None = None
    status: Literal[
        "equal", "changed", "left_only", "right_only", "ambiguous", "unavailable"
    ]
    left_state: EvidenceState
    right_state: EvidenceState
    left: dict[str, Any] | None
    right: dict[str, Any] | None
    match_basis: Literal[
        "source_event",
        "semantic_multiset",
        "observed_timestamp",
        "window_metric",
        "none",
    ]
    ambiguous: bool = False
    associations: list[ComparisonAssociation] = Field(default_factory=list)
    missing_reason: str | None = None

    @model_validator(mode="after")
    def known_section(self):
        if self.section not in SECTIONS:
            raise ValueError("Unknown comparison section")
        return self


class ComparisonArtifactRef(Contract):
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]+$", max_length=200)
    sha256: Sha256
    size_bytes: int = Field(ge=0)
    rows: int = Field(ge=0)
    media_type: Literal["application/vnd.apache.parquet"] = (
        "application/vnd.apache.parquet"
    )
    section: str
    # A bounded block index enables exact event reads without scanning the whole
    # candidate comparison or depending on browser offset pagination.
    event_pair_ids: list[str] = Field(default_factory=list, max_length=16384)
    first_row: int = Field(ge=0)
    last_row: int = Field(ge=0)


class ComparisonManifest(Contract):
    schema_version: Literal["tradegrad.comparison.v1"] = "tradegrad.comparison.v1"
    publication: ComparisonPublication
    input_hash: Sha256
    left: ComparisonSidePins
    right: ComparisonSidePins
    algorithm_version: ComparisonAlgorithmVersion = ALGORITHM_VERSION
    tolerance_version: Literal["tradegrad.comparison-tolerances.v1"] = TOLERANCE_VERSION
    field_tolerances: dict[str, dict[str, float]]
    limits: ComparisonLimits
    namespace_status: Literal["common", "different", "unknown"]
    namespace_reason: str
    pairing_fields: list[str]
    code_changed: bool
    configuration_differences: list[ConfigurationDifference]
    mechanism_changes: list[MechanismChange]
    sections: dict[str, ComparisonSectionSummary]
    observed_behavior: Literal["changed", "unchanged_observed", "insufficient_coverage"]
    observed_equal_sections: list[str]
    diagnostic_parity_certified: Literal[False] = False
    local_counterfactual: Literal[False] = False
    causal_claim: Literal[False] = False
    artifacts: list[ComparisonArtifactRef]
    warnings: list[str]

    @model_validator(mode="after")
    def exact_sections(self):
        if set(self.sections) != set(SECTIONS):
            raise ValueError(
                "Every comparison section needs an explicit availability declaration"
            )
        if any(ref.section not in SECTIONS for ref in self.artifacts):
            raise ValueError("Unknown comparison artifact section")
        if len({ref.name for ref in self.artifacts}) != len(self.artifacts):
            raise ValueError("Duplicate comparison artifacts")
        return self


class ComparisonPage(Contract):
    section: str
    availability: ComparisonSectionSummary
    items: list[ComparisonRecord]
    total: int
    offset: int
    limit: int


@dataclass(frozen=True)
class WrittenComparisonArtifact:
    name: str
    path: Path
    sha256: str
    size_bytes: int
    media_type: str
    rows: int
    section: str | None
