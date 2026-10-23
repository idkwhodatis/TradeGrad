"""Fail-closed service configuration, separate from the quant runtime."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")
    database_url: str
    secret_key: str = Field(min_length=32)
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
