"""Fail-closed service configuration, separate from the quant runtime."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from backend.app.operations_schemas import BackupReceipt
from packages.tradegrad_adapter.model_profiles import (
    OperatorModelProfile,
    validate_registry,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", hide_input_in_errors=True
    )
    database_url: str = Field(repr=False)
    secret_key: str = Field(min_length=32, repr=False)
    storage_root: Path = Path("/var/lib/tradegrad")
    access_token_minutes: int = Field(default=30, ge=1, le=1440)
    lease_seconds: int = Field(default=60, ge=10, le=3600)
    heartbeat_seconds: int = Field(default=10, ge=1, le=60)
    sse_poll_seconds: float = Field(default=1, ge=0.1, le=30)
    cors_origins: list[str] = []
    max_artifact_bytes: int = Field(default=50_000_000, ge=1024)
    data_provider_mode: str = Field(default="live", pattern=r"^(live|fixture)$")
    data_lease_seconds: int = Field(default=120, ge=30, le=600)
    data_block_days: int = Field(default=31, ge=1, le=366)
    max_active_imports_per_user: int = Field(default=4, ge=1, le=100)
    max_upload_bytes: int = Field(default=10_000_000, ge=1024, le=10_000_000)
    max_active_runs_per_user: int = Field(default=4, ge=1, le=100)

    # Sanitized JSON receipts are injected by the operator, never loaded from backup paths.
    operations_application_ref: str | None = Field(
        default=None, pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$"
    )
    operations_backup_receipts: tuple[BackupReceipt, ...] = Field(
        default=(), max_length=16
    )
    operations_receipt_max_age_seconds: int = Field(default=86400, ge=60, le=2592000)

    model_profiles_enabled: bool = False
    model_profiles: tuple[OperatorModelProfile, ...] = Field(
        default=(), max_length=64, repr=False
    )
    model_synthetic_test_mode: bool = False

    @model_validator(mode="after")
    def model_registry(self):
        validate_registry(self)
        return self

    @field_validator("database_url")
    @classmethod
    def postgres_only(cls, value: str) -> str:
        url = make_url(value)
        if url.drivername not in {"postgresql", "postgresql+psycopg"}:
            raise ValueError(
                "Only PostgreSQL with psycopg is supported; SQLite is not a substitute"
            )
        return str(
            url.set(drivername="postgresql+psycopg").render_as_string(
                hide_password=False
            )
        )

    @field_validator("secret_key")
    @classmethod
    def reject_placeholder_secret(cls, value: str) -> str:
        if value.lower() in {"changethis" * 4, "change-me" * 4}:
            raise ValueError("Use a randomly generated SECRET_KEY")
        return value

    @property
    def psycopg_url(self) -> str:
        return (
            make_url(self.database_url)
            .set(drivername="postgresql")
            .render_as_string(hide_password=False)
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
