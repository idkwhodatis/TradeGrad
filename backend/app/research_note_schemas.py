"""Human-authored notes and discriminated, immutable evidence references.

No evidence kind accepts another kind's identifiers. UI restoration is only a
bounded view of these references, never an alternate admission mechanism.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from backend.app.comparison_schemas import ComparisonSideCreate
from packages.research_trace.contracts import (
    Contract,
    EvaluationBinding,
    Identifier,
    Sha256,
    SourceRef,
    canonical_bytes,
)

NoteStatus = Literal[
    "pending", "supported_under_current_tests", "mixed", "refuted_under_current_tests"
]


class NoteSourcePin(Contract):
    source_kind: Literal["strategy_version", "candidate"]
    source_id: UUID
    code_sha256: Sha256
    spec_revision: int | None = Field(default=None, ge=1, le=1_000_000)
    spec_sha256: Sha256 | None = None
    rule_id: Identifier | None = None
    source_ref: SourceRef | None = None

    @model_validator(mode="after")
    def exact(self):
        if (self.spec_revision is None) != (self.spec_sha256 is None):
            raise ValueError("Description revision and hash must be paired")
        if self.rule_id and self.spec_revision is None:
            raise ValueError("Rule evidence requires its exact description revision")
        if self.source_ref and self.source_ref.code_sha256 != self.code_sha256:
            raise ValueError("Source anchor must pin the same immutable code")
        return self


class NoteSourceEvidence(NoteSourcePin):
    kind: Literal["source"]
    label: str = Field(default="", max_length=200)


class NoteEvaluationEvidence(Contract):
    kind: Literal["evaluation", "decision"]
    label: str = Field(default="", max_length=200)
    leaf: ComparisonSideCreate
    decision_id: Identifier | None = None
    instrument_id: Identifier | None = None
    stage_id: Identifier | None = None
    session_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    record_kind: Literal["candidate", "feature", "predicate", "order"] | None = None
    record_id: Identifier | None = None
    record_ordinal: int | None = Field(default=None, ge=0, le=2**53 - 1, strict=True)
    source_ref: SourceRef | None = None

    @model_validator(mode="after")
    def exact(self):
        if self.kind == "decision" and not self.decision_id:
            raise ValueError("Decision evidence requires its immutable decision ID")
        if self.kind == "evaluation" and any(
            value is not None
            for value in (
                self.decision_id,
                self.instrument_id,
                self.stage_id,
                self.session_date,
                self.record_kind,
                self.record_id,
                self.record_ordinal,
            )
        ):
            raise ValueError("Observation selectors belong to decision evidence")
        if self.decision_id and self.leaf.trace_manifest_sha256 is None:
            raise ValueError("Decision evidence requires an exact trace manifest")
        if (self.record_kind is None) != (self.record_id is None):
            raise ValueError("Observation kind and ID must be paired")
        if self.record_ordinal is not None and not (self.stage_id and self.record_kind):
            raise ValueError("Observation ordinal requires its recorded stage and kind")
        if self.source_ref and self.source_ref.code_sha256 != self.leaf.code_sha256:
            raise ValueError("Observation anchor must pin the actual executed code")
        return self


class NoteComparisonEvidence(Contract):
    kind: Literal["comparison"]
    label: str = Field(default="", max_length=200)
    comparison_id: UUID
    input_sha256: Sha256
    manifest_sha256: Sha256
    left: ComparisonSideCreate
    right: ComparisonSideCreate
    event_pair_id: Identifier | None = None
    section: Literal[
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
    ] = "events"
    instrument_id: Identifier | None = None
    mechanism_index: int | None = Field(default=None, ge=0, le=100000, strict=True)


class NoteDiagnosticEvidence(Contract):
    kind: Literal["diagnostic"]
    label: str = Field(default="", max_length=200)
    run_id: UUID
    config_sha256: Sha256
    report_sha256: Sha256
    result: ComparisonSideCreate


class NoteControlledArm(Contract):
    ordinal: int = Field(ge=0, le=2, strict=True)
    run_id: UUID
    status: Literal["succeeded", "failed", "cancelled", "interrupted"]
    # Failed arms preserve the exact stopped attempt, never a zero result.
    execution_attempt_id: UUID
    generation: int = Field(ge=1, strict=True)
    result: ComparisonSideCreate | None

    @model_validator(mode="after")
    def exact(self):
        if (self.status == "succeeded") != (self.result is not None):
            raise ValueError("Only successful arms have immutable economic results")
        if (
            self.result
            and self.result.execution_attempt_id != self.execution_attempt_id
        ):
            raise ValueError("Control result and stopped attempt must agree")
        return self


class NoteControlledEvidence(Contract):
    kind: Literal["controlled"]
    label: str = Field(default="", max_length=200)
    controlled_experiment_id: UUID
    request_sha256: Sha256
    protocol_sha256: Sha256
    arms: tuple[NoteControlledArm, ...] = Field(min_length=2, max_length=3)
    selected_arm: int | None = Field(default=None, ge=0, le=2, strict=True)

    @model_validator(mode="after")
    def ordered(self):
        if [arm.ordinal for arm in self.arms] != list(range(len(self.arms))):
            raise ValueError("Control evidence includes every arm in original order")
        if self.selected_arm is not None and self.selected_arm >= len(self.arms):
            raise ValueError("Selected control arm does not exist")
        return self


NoteEvidence = Annotated[
    NoteSourceEvidence
    | NoteEvaluationEvidence
    | NoteComparisonEvidence
    | NoteDiagnosticEvidence
    | NoteControlledEvidence,
    Field(discriminator="kind"),
]


class NoteRestore(Contract):
    strategy_version_id: UUID
    # Scalar-only known URL keys are validated again against admitted evidence.
    selection: dict[str, str | int | float] = Field(max_length=120)

    @field_validator("selection", mode="before")
    @classmethod
    def bounded(cls, value):
        from backend.app.research_note_selection import validate_selection

        if not isinstance(value, dict):
            raise ValueError("Research selection must be an object")  # noqa: TRY004 - Pydantic validation
        validate_selection(value)
        return value


class ResearchNoteContent(Contract):
    title: str = Field(min_length=1, max_length=240)
    tags: tuple[str, ...] = Field(default=(), max_length=20)
    status: NoteStatus = "pending"
    body: str = Field(default="", max_length=32000)
    hypothesis: str = Field(default="", max_length=8000)
    motivation: str = Field(default="", max_length=8000)
    rules: str = Field(default="", max_length=8000)
    sources: str = Field(default="", max_length=8000)
    behavior_evidence: str = Field(default="", max_length=8000)
    control_results: str = Field(default="", max_length=8000)
    counterexamples: str = Field(default="", max_length=8000)
    applicability: str = Field(default="", max_length=8000)
    next_steps: str = Field(default="", max_length=8000)
    evidence: tuple[NoteEvidence, ...] = Field(default=(), max_length=32)
    restore: NoteRestore | None = None

    @model_validator(mode="after")
    def bounded(self):
        if not self.title.strip():
            raise ValueError("Title must contain visible text")
        if any(not tag.strip() or len(tag) > 80 for tag in self.tags):
            raise ValueError("Tags need 1 to 80 visible characters")
        if len({tag.casefold() for tag in self.tags}) != len(self.tags):
            raise ValueError("Tags must be distinct")
        if len(canonical_bytes(self)) > 128 * 1024:
            raise ValueError("Note content exceeds its 128 KiB byte budget")
        return self


class ResearchNoteCreate(Contract):
    content: ResearchNoteContent
    change_note: str | None = Field(default=None, max_length=2000)


class ResearchNoteEdit(ResearchNoteCreate):
    base_revision: int = Field(ge=1, le=10000, strict=True)
    base_revision_sha256: Sha256


class NoteEvidenceResolution(Contract):
    index: int
    state: Literal["available", "missing", "unavailable", "corrupt"]
    reason: str | None
    bindings: list[EvaluationBinding] = Field(default_factory=list)
    # Never return a link for failed admission or from caller-supplied URL text.
    href: str | None = None


class ResearchNoteSummary(Contract):
    id: UUID
    revision: int
    revision_sha256: Sha256
    title: str
    tags: tuple[str, ...]
    status: NoteStatus
    evidence_count: int
    created_at: datetime
    updated_at: datetime
    change_note: str | None


class ResearchNoteRevision(Contract):
    id: UUID
    note_id: UUID
    revision: int
    revision_sha256: Sha256
    created_at: datetime
    change_note: str | None
    content: ResearchNoteContent
    evidence: list[NoteEvidenceResolution]
    restore_href: str | None
    retained_pin_count: int
    retention_reason: Literal["immutable_note_revision"] = "immutable_note_revision"
    status_basis: Literal["human_maintained_not_automatic_validation"] = (
        "human_maintained_not_automatic_validation"
    )
