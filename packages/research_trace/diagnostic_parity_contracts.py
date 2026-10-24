"""Immutable independent replay parity, not an I4 comparison or a causal claim."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from .comparison_contracts import FIELD_TOLERANCES
from .contracts import Contract, EvaluationBinding, Sha256

PARITY_VERSION = "tradegrad.diagnostic-parity.v1"
PARITY_ALGORITHM = "tradegrad.original-start-parity.v1"
PARITY_TOLERANCES = "tradegrad.diagnostic-tolerances.v1"
MAX_PARITY_BYTES = 16 * 1024**2
MAX_PARITY_EVENTS = 10000
MAX_PARITY_INPUT_BYTES = 64 * 1024**2
MAX_MISMATCH_EXAMPLES = 20
REQUIRED_CHECKS = (
    "source_identity",
    "fixed_conditions",
    "rng_reconstruction",
    "randomness_scope",
    "audit_coverage",
    "submitted_intents",
    "ordered_status_history",
    "fills",
    "position_trajectory",
    "account_trajectory",
    "result_metrics",
    "original_event_mapping",
    "decision_pre_states",
    "observed_targets",
)
ParityState = Literal["matched", "mismatch", "not_verifiable"]


class DiagnosticParitySide(Contract):
    binding: EvaluationBinding
    source_kind: Literal["candidate", "strategy_version"]
    source_id: UUID
    spec_revision: int | None
    spec_sha256: Sha256 | None
    trace_manifest_sha256: Sha256 | None
    runtime_observation_sha256: Sha256 | None

    @model_validator(mode="after")
    def paired_spec(self):
        if (self.spec_revision is None) != (self.spec_sha256 is None):
            raise ValueError("Parity spec revision/hash must be paired")
        return self


class DiagnosticParityCheck(Contract):
    key: str
    status: ParityState
    definition: str
    reason: str | None = None
    source_count: int | None = Field(default=None, ge=0)
    replay_count: int | None = Field(default=None, ge=0)
    mismatch_count: int = Field(default=0, ge=0)
    examples: list[dict[str, Any]] = Field(
        default_factory=list, max_length=MAX_MISMATCH_EXAMPLES
    )


class DiagnosticEventPair(Contract):
    pairing_key: tuple[str, str, str, int, str | None]
    original_decision_id: str | None
    replay_decision_id: str | None
    original_pre_state_sha256: Sha256 | None
    replay_pre_state_sha256: Sha256 | None
    pre_state_status: ParityState
    observation_status: ParityState
    mapping: Literal[
        "verified_original_event",
        "unverified_observed_pairing",
        "unpaired",
        "ambiguous",
    ]
    reason: str | None = None

    @model_validator(mode="after")
    def truthful_mapping(self):
        if self.mapping in {
            "verified_original_event",
            "unverified_observed_pairing",
        } and (
            not self.original_decision_id
            or not self.replay_decision_id
            or self.original_decision_id == self.replay_decision_id
        ):
            raise ValueError(
                "Paired diagnostic events require distinct original and replay IDs"
            )
        if self.pre_state_status == "matched" and (
            self.original_pre_state_sha256 is None
            or self.replay_pre_state_sha256 is None
        ):
            raise ValueError("Matched pre-states require both actually retained states")
        if self.mapping == "verified_original_event" and (
            self.pre_state_status != "matched" or self.observation_status != "matched"
        ):
            raise ValueError(
                "Verified event mappings require matched retained observations"
            )
        return self


class DiagnosticParityReport(Contract):
    schema_version: Literal["tradegrad.diagnostic-parity.v1"] = PARITY_VERSION
    algorithm_version: Literal["tradegrad.original-start-parity.v1"] = PARITY_ALGORITHM
    tolerance_version: Literal["tradegrad.diagnostic-tolerances.v1"] = PARITY_TOLERANCES
    source: DiagnosticParitySide
    replay: DiagnosticParitySide
    outcome: ParityState
    checks: list[DiagnosticParityCheck]
    events: list[DiagnosticEventPair] = Field(max_length=MAX_PARITY_EVENTS)
    original_event_count: int = Field(ge=0)
    replay_event_count: int = Field(ge=0)
    observation_scope: dict[str, Any]
    field_tolerances: dict[str, dict[str, float]] = Field(
        default_factory=lambda: FIELD_TOLERANCES.copy()
    )
    claim: Literal["scoped_observed_reexecution_parity_not_causal_or_ledger_proof"] = (
        "scoped_observed_reexecution_parity_not_causal_or_ledger_proof"
    )
    limitations: list[str]

    @model_validator(mode="after")
    def truthful(self):
        expected = (
            "mismatch"
            if any(c.status == "mismatch" for c in self.checks)
            else "not_verifiable"
            if not self.checks or any(c.status == "not_verifiable" for c in self.checks)
            else "matched"
        )
        if self.outcome != expected or (
            self.outcome != "matched"
            and any(e.mapping == "verified_original_event" for e in self.events)
        ):
            raise ValueError("Parity certification differs from named checks")
        if {c.key for c in self.checks} != set(REQUIRED_CHECKS) or len(
            self.checks
        ) != len(REQUIRED_CHECKS):
            raise ValueError("Parity requires every named check exactly once")
        if self.field_tolerances != FIELD_TOLERANCES:
            raise ValueError("Parity tolerances cannot be supplied by an execution")
        return self


class DiagnosticParityView(Contract):
    report_sha256: Sha256
    schema_version: Literal["tradegrad.diagnostic-parity.v1"] = PARITY_VERSION
    algorithm_version: str
    tolerance_version: str
    source: DiagnosticParitySide
    replay: DiagnosticParitySide
    outcome: ParityState
    checks: list[DiagnosticParityCheck]
    original_event_count: int
    replay_event_count: int
    mapping_counts: dict[str, int]
    observation_scope: dict[str, Any]
    field_tolerances: dict[str, dict[str, float]]
    claim: str
    limitations: list[str]


class DiagnosticParityEventPage(Contract):
    report_sha256: Sha256
    items: list[DiagnosticEventPair] = Field(max_length=500)
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=500)


@dataclass(frozen=True)
class ParityInput:
    pins: DiagnosticParitySide
    source_code: str
    bundle: Mapping[str, Any]
    runtime_observation: Mapping[str, Any] | None
    fixed_conditions: Mapping[str, Any]
    # Full retained decision headers/pre-states and target/plan stages only.
    # Feature/candidate/predicate detail never needs materialization for parity.
    decisions: tuple[Mapping[str, Any], ...]
    capture: Mapping[str, Any] | None
    capture_missing: tuple[str, ...] = ()
