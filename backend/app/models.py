"""PostgreSQL metadata. All resource identity and ownership comes from the database."""

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(Text)
    full_name: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    market: Mapped[str] = mapped_column(String(2))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)  # exclusive
    source: Mapped[str] = mapped_column(String(80), default="synthetic_fixture")
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    content_sha256: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    storage_key: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    __table_args__ = (
        CheckConstraint("market IN ('CN', 'US')"),
        CheckConstraint("start_date < end_date"),
    )


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    task: Mapped[str] = mapped_column(String(20))
    mode: Mapped[str] = mapped_column(String(30), default="native_akquant")
    content_sha256: Mapped[str] = mapped_column(String(64))
    code: Mapped[str] = mapped_column(Text)
    parent_id: Mapped[UUID | None] = mapped_column(ForeignKey("strategy_versions.id"))
    manifest: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    parameters: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class ResearchRun(Base):
    __tablename__ = "research_runs"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_sha256: Mapped[str] = mapped_column(String(64))
    submission_binding: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    dataset_version_id: Mapped[UUID] = mapped_column(ForeignKey("dataset_versions.id"))
    strategy_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("strategy_versions.id")
    )
    experiment_id: Mapped[UUID | None] = mapped_column(ForeignKey("experiments.id"))
    split_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("split_versions.id")
    )
    final_selection_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("final_selections.id", use_alter=True, name="fk_run_final_selection")
    )
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    active_attempt_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    research_image_digest: Mapped[str | None] = mapped_column(String(255))
    portable_policy_image_digest: Mapped[str | None] = mapped_column(String(255))
    latest_checkpoint_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "research_checkpoints.id", use_alter=True, name="fk_run_latest_checkpoint"
        )
    )
    resume_checkpoint_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "research_checkpoints.id", use_alter=True, name="fk_run_resume_checkpoint"
        )
    )
    next_event_seq: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    __table_args__ = (
        UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_run_owner_idempotency"
        ),
        CheckConstraint("generation > 0"),
        CheckConstraint(
            "status IN ('queued','preparing','running','finalizing','pause_requested','paused','cancel_requested','cancelled','failed','interrupted','succeeded')"
        ),
    )

    # No persisted duplicate of this value: immutable config is authoritative.
    @property
    def execution_mode(self):
        return self.config.get("execution_mode", "seed_evaluation")


class ExecutionAttempt(Base):
    __tablename__ = "execution_attempts"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    run_id: Mapped[UUID] = mapped_column(ForeignKey("research_runs.id"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str] = mapped_column(String(255))
    resumed_checkpoint_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "research_checkpoints.id",
            use_alter=True,
            name="fk_attempt_resumed_checkpoint",
        )
    )
    lease_token: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), default=uuid4)
    lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    status: Mapped[str] = mapped_column(String(30), default="preparing")
    sandbox_id: Mapped[str] = mapped_column(String(255))
    sandbox_stopped: Mapped[bool] = mapped_column(Boolean, default=False)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("run_id", "generation", name="uq_attempt_run_generation"),
    )


class RunEvent(Base):
    __tablename__ = "run_events"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    run_id: Mapped[UUID] = mapped_column(ForeignKey("research_runs.id"), index=True)
    attempt_id: Mapped[UUID | None] = mapped_column(ForeignKey("execution_attempts.id"))
    seq: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    type: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_event_run_seq"),)


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    run_id: Mapped[UUID] = mapped_column(ForeignKey("research_runs.id"), index=True)
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("execution_attempts.id"))
    name: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(100))
    storage_key: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    __table_args__ = (
        UniqueConstraint("attempt_id", "name", name="uq_artifact_attempt_name"),
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    run_id: Mapped[UUID] = mapped_column(ForeignKey("research_runs.id"))
    generation: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    job_id: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (
        UniqueConstraint("run_id", "generation", name="uq_outbox_run_generation"),
        Index("ix_outbox_pending", "dispatched_at", "created_at"),
    )


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    role: Mapped[str] = mapped_column(String(40))
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class Instrument(Base):
    __tablename__ = "instruments"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    asset_type: Mapped[str] = mapped_column(String(30))
    identity_status: Mapped[str] = mapped_column(
        String(30), default="provider_resolved"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class Listing(Base):
    __tablename__ = "listings"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.id"))
    symbol: Mapped[str] = mapped_column(String(80))
    mic: Mapped[str] = mapped_column(String(4))
    currency: Mapped[str] = mapped_column(String(16))
    timezone: Mapped[str] = mapped_column(String(80))
    calendar: Mapped[str] = mapped_column(String(80))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class OwnerListing(Base):
    __tablename__ = "owner_listings"
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    listing_id: Mapped[UUID] = mapped_column(
        ForeignKey("listings.id"), primary_key=True
    )


class VendorMapping(Base):
    __tablename__ = "vendor_mappings"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    listing_id: Mapped[UUID] = mapped_column(ForeignKey("listings.id"), index=True)
    vendor: Mapped[str] = mapped_column(String(80))
    symbol: Mapped[str] = mapped_column(String(80))
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    __table_args__ = (
        UniqueConstraint(
            "listing_id",
            "vendor",
            "symbol",
            "valid_from",
            "valid_to",
            name="uq_listing_vendor_period",
        ),
    )


class UniverseVersion(Base):
    __tablename__ = "universe_versions"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(40))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    members: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    definition: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    __table_args__ = (
        CheckConstraint(
            "state IN ('user_fixed','current_snapshot','historical_membership','point_in_time_verified')"
        ),
    )


class DataImport(Base):
    __tablename__ = "data_imports"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str] = mapped_column(String(30))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_sha256: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    listing_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(30), default="requested", index=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    lease_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    dataset_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("dataset_versions.id")
    )
    parent_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("dataset_versions.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    __table_args__ = (
        UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_data_import_owner_key"
        ),
        CheckConstraint(
            "status IN ('requested','fetching','normalizing','validating','ready','partial','failed','cancelled')"
        ),
        CheckConstraint("generation > 0"),
    )


class DataBlock(Base):
    __tablename__ = "data_blocks"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    import_id: Mapped[UUID] = mapped_column(ForeignKey("data_imports.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(80))
    listing_id: Mapped[UUID | None] = mapped_column(ForeignKey("listings.id"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(30), default="requested")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    storage_key: Mapped[str | None] = mapped_column(String(500))
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    coverage: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    __table_args__ = (
        UniqueConstraint(
            "import_id", "symbol", "start_date", "end_date", name="uq_data_block_window"
        ),
    )


class DataOutbox(Base):
    __tablename__ = "data_outbox"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    import_id: Mapped[UUID] = mapped_column(ForeignKey("data_imports.id"))
    generation: Mapped[int] = mapped_column(Integer)
    job_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("import_id", "generation", name="uq_data_outbox_generation"),
    )


class ProviderBudget(Base):
    __tablename__ = "provider_budgets"
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    next_allowed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Candidate(Base):
    """Immutable code/lineage version. Repair never overwrites its input."""

    __tablename__ = "research_candidates"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    run_id: Mapped[UUID] = mapped_column(ForeignKey("research_runs.id"), index=True)
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("execution_attempts.id"))
    generation: Mapped[int] = mapped_column(Integer)
    candidate_key: Mapped[str] = mapped_column(String(160))
    parent_id: Mapped[UUID | None] = mapped_column(ForeignKey("research_candidates.id"))
    kind: Mapped[str] = mapped_column(String(20))
    iteration: Mapped[int] = mapped_column(Integer)
    repair_index: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(Text)
    code_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    __table_args__ = (
        UniqueConstraint("run_id", "candidate_key", name="uq_candidate_run_key"),
        CheckConstraint("kind IN ('seed','generated','repair')"),
        CheckConstraint("generation > 0 AND iteration >= 0 AND repair_index >= 0"),
    )


class Evaluation(Base):
    """Sandbox registration is durable before launch; completion payload is immutable."""

    __tablename__ = "research_evaluations"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    run_id: Mapped[UUID] = mapped_column(ForeignKey("research_runs.id"), index=True)
    attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("execution_attempts.id"), index=True
    )
    generation: Mapped[int] = mapped_column(Integer)
    candidate_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("research_candidates.id")
    )
    candidate_key: Mapped[str] = mapped_column(String(160))
    code_sha256: Mapped[str] = mapped_column(String(64))
    split: Mapped[str] = mapped_column(String(20))
    window: Mapped[dict[str, Any]] = mapped_column(JSONB)
    window_sha256: Mapped[str] = mapped_column(String(64))
    image_digest: Mapped[str] = mapped_column(String(255))
    sandbox_id: Mapped[str] = mapped_column(String(255), unique=True)
    sandbox_stopped: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(30), default="registered")
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    payload_sha256: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("split IN ('train','validation','holdout','backtest')"),
        CheckConstraint(
            "status IN ('registered','succeeded','failed','cancelled','interrupted')"
        ),
        CheckConstraint("generation > 0"),
    )


class ModelCall(Base):
    __tablename__ = "research_model_calls"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    run_id: Mapped[UUID] = mapped_column(ForeignKey("research_runs.id"), index=True)
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("execution_attempts.id"))
    generation: Mapped[int] = mapped_column(Integer)
    logical_call_id: Mapped[str] = mapped_column(String(160))
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(160))
    prompt_version: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(30))
    adopted: Mapped[bool] = mapped_column(Boolean)
    request_sha256: Mapped[str] = mapped_column(String(64))
    response_sha256: Mapped[str | None] = mapped_column(String(64))
    response_artifact: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB)
    latency_seconds: Mapped[float] = mapped_column(Float)
    error_category: Mapped[str | None] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    __table_args__ = (
        UniqueConstraint("run_id", "logical_call_id", name="uq_model_call_run_logical"),
    )


class ResearchCheckpoint(Base):
    __tablename__ = "research_checkpoints"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    run_id: Mapped[UUID] = mapped_column(ForeignKey("research_runs.id"), index=True)
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("execution_attempts.id"))
    generation: Mapped[int] = mapped_column(Integer)
    sequence: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[int] = mapped_column(Integer)
    manifest_storage_key: Mapped[str] = mapped_column(String(500))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    input_digest: Mapped[str] = mapped_column(String(64))
    config_sha256: Mapped[str] = mapped_column(String(64))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB)
    next_stage: Mapped[str] = mapped_column(String(80))
    iteration: Mapped[int] = mapped_column(Integer)
    complete: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_checkpoint_run_sequence"),
        UniqueConstraint(
            "attempt_id", "manifest_sha256", name="uq_checkpoint_attempt_manifest"
        ),
        CheckConstraint("complete = true AND generation > 0 AND sequence > 0"),
    )


class Experiment(Base):
    """Immutable family identity. A new family never resets owner exposure history."""

    __tablename__ = "experiments"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    root_experiment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("experiments.id")
    )
    parent_experiment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("experiments.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class SplitVersion(Base):
    __tablename__ = "split_versions"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiments.id"), index=True
    )
    dataset_version_id: Mapped[UUID] = mapped_column(ForeignKey("dataset_versions.id"))
    universe_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("universe_versions.id")
    )
    content_sha256: Mapped[str] = mapped_column(String(64))
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class FinalSelection(Base):
    __tablename__ = "final_selections"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    experiment_id: Mapped[UUID] = mapped_column(ForeignKey("experiments.id"))
    split_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("split_versions.id"), unique=True
    )
    source_run_id: Mapped[UUID] = mapped_column(ForeignKey("research_runs.id"))
    source_candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_candidates.id")
    )
    source_checkpoint_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_checkpoints.id")
    )
    strategy_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("strategy_versions.id")
    )
    request_sha256: Mapped[str] = mapped_column(String(64))
    source_config_sha256: Mapped[str] = mapped_column(String(64))
    source_code_sha256: Mapped[str] = mapped_column(String(64))
    source_checkpoint_sha256: Mapped[str] = mapped_column(String(64))
    selection_evidence: Mapped[dict[str, Any]] = mapped_column(JSONB)
    exposure_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class DataUsageExposure(Base):
    """Append-only conservative access/admission evidence, never a cleanliness proof."""

    __tablename__ = "data_usage_exposures"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    experiment_id: Mapped[UUID | None] = mapped_column(ForeignKey("experiments.id"))
    split_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("split_versions.id")
    )
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("research_runs.id"))
    dataset_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("dataset_versions.id")
    )
    source_key: Mapped[str] = mapped_column(String(255))
    identities: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    purpose: Mapped[str] = mapped_column(String(80))
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    __table_args__ = (
        UniqueConstraint("owner_id", "source_key", name="uq_exposure_owner_source"),
        CheckConstraint("start_date < end_date"),
        Index("ix_exposure_owner_window", "owner_id", "start_date", "end_date"),
    )


class StrategyPackage(Base):
    """Standalone immutable export, not a fake worker Artifact/ExecutionAttempt."""

    __tablename__ = "strategy_packages"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    final_selection_id: Mapped[UUID] = mapped_column(ForeignKey("final_selections.id"))
    request_sha256: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_sha256: Mapped[str] = mapped_column(String(64))
    archive_sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(500))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    exposure_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    exposure_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    __table_args__ = (
        UniqueConstraint("owner_id", "request_sha256", name="uq_package_owner_request"),
        CheckConstraint("size_bytes > 0"),
    )
