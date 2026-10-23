"""Strict management contracts. No provider endpoint, environment name or key input."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class ManagementInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class WorkspaceDefaults(ManagementInput):
    initial_cash: float = Field(
        default=1_000_000, gt=0, le=1_000_000_000, allow_inf_nan=False
    )
    max_iterations: int = Field(default=10, ge=0, le=1000)
    max_model_calls: int = Field(default=100, ge=0, le=10000)
    max_tokens: int = Field(default=1_000_000, ge=0, le=100_000_000)
    max_runtime_seconds: int = Field(default=3600, ge=1, le=86400)
    model_profile_id: str = Field(default="fixture", pattern=r"^[a-z][a-z0-9_-]{0,63}$")


class SelfProfileUpdate(ManagementInput):
    full_name: str | None = Field(default=None, max_length=255)

    @field_validator("full_name")
    @classmethod
    def name(cls, value):
        if value is not None and any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Name must not contain control characters")
        return value.strip() or None if value is not None else None


class ManagedUserCreate(SelfProfileUpdate):
    email: EmailStr
    password: str = Field(min_length=12, max_length=1024, repr=False)
    is_active: bool = True
    is_superuser: bool = False


class ManagedUserUpdate(SelfProfileUpdate):
    is_active: bool
    is_superuser: bool


class QuotaUpdate(ManagementInput):
    max_active_runs: int | None = Field(default=None, ge=0, le=100)
    max_active_imports: int | None = Field(default=None, ge=0, le=100)


class QuotaPublic(QuotaUpdate):
    user_id: UUID
    effective_active_runs: int
    effective_active_imports: int
    deployment_active_runs: int
    deployment_active_imports: int
    active_runs: int
    active_imports: int


class SecretReferenceUpdate(ManagementInput):
    enabled: bool


class SecretReferencePublic(BaseModel):
    id: str
    label: str
    provider: str
    model: str
    enabled: bool
    deployment_enabled: bool
    synthetic: bool


class SettingsStatus(BaseModel):
    permissions: list[str]
    dependencies: dict[str, str]
    schema_revision: str
    backup_status: Literal["not_reported"] = "not_reported"
    backup_note: str = "No verified backup receipt is registered. Use the operator cold-backup and restore drill; heartbeat or schema readiness does not prove backup coverage."
    secret_reference_boundary: str = "Only disable or re-enable existing operator-allowlisted profile references for their already listed owners. No endpoint, environment reference, key, credential rotation or allowlist grant is accepted. Revocation stops new admissions and worker model-call boundaries, not an already admitted external call."
