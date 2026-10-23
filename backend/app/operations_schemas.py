"""Bounded aggregate observations, never raw logs, private paths or backup bytes."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BackupReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    format: Literal["tradegrad.backup-receipt.v1"] = "tradegrad.backup-receipt.v1"
    operation: Literal["create", "verify", "restore"]
    application_ref: str = Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
    schema_revision: Literal["003_m3", "004_m5", "005_settings"]
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    backup_created_at: datetime
    observed_at: datetime

    @field_validator("backup_created_at", "observed_at")
    @classmethod
    def aware(cls, value):
        if value.utcoffset() is None:
            raise ValueError("Receipt times must have timezones")
        return value


class BackupEvidence(BaseModel):
    receipt: BackupReceipt
    state: Literal[
        "observed", "stale", "version_mismatch", "invalid_time", "identity_unavailable"
    ]
    age_seconds: float | None = None
    backup_age_seconds: float | None = None


class DurationObservation(BaseModel):
    count: int = 0
    unavailable_count: int = 0
    mean_seconds: float | None = None
    p95_seconds: float | None = None
    max_seconds: float | None = None


class ImportObservation(BaseModel):
    sampled_completed: int
    ready: int
    failed: int
    partial: int
    cancelled: int
    failure_fraction: float | None
    unsuccessful_fraction: float | None
    truncated: bool


class UsageObservation(BaseModel):
    calls: int
    known_usage_calls: int
    unknown_usage_calls: int
    input_tokens: int | None
    output_tokens: int | None
    charged_tokens: int | None
    invalid_usage_calls: int
    truncated: bool


class DiskObservation(BaseModel):
    state: Literal["observed", "restricted", "unavailable"]
    capacity_bytes: int | None = None
    free_bytes: int | None = None
    available_bytes: int | None = None


class OperationsPublic(BaseModel):
    observed_at: datetime
    window_start: datetime
    scope: Literal["owner", "deployment"]
    sample_limit: int
    observation_ttl_seconds: int = 60
    queued_runs: int
    queued_imports: int
    active_runs: int
    active_imports: int
    queue_counts_truncated: bool
    current_queue_age: DurationObservation
    queue_wait: DurationObservation
    attempt_runtime: DurationObservation
    cancellation_latency: DurationObservation
    attempts_truncated: bool
    imports: ImportObservation
    model_usage: UsageObservation
    disk: DiskObservation
    notes: list[str]
