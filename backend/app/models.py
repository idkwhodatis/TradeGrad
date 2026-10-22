"""PostgreSQL metadata. All resource identity and ownership comes from the database."""

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
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
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    dataset_version_id: Mapped[UUID] = mapped_column(ForeignKey("dataset_versions.id"))
    strategy_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("strategy_versions.id")
    )
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    active_attempt_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
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
            "status IN ('queued','preparing','running','finalizing','cancel_requested','cancelled','failed','interrupted','succeeded')"
        ),
    )


class ExecutionAttempt(Base):
    __tablename__ = "execution_attempts"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    run_id: Mapped[UUID] = mapped_column(ForeignKey("research_runs.id"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str] = mapped_column(String(255))
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
