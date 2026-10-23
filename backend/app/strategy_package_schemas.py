"""Owner-only reference exports. No arbitrary paths or raw-data export options."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from backend.app.schemas import PublicModel


class StrategyPackageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference_evaluation_id: UUID
    profile: Literal["reference_only.v1"] = "reference_only.v1"


class StrategyPackagePublic(PublicModel):
    id: UUID
    final_selection_id: UUID
    request_sha256: str
    content_sha256: str
    archive_sha256: str
    size_bytes: int
    manifest: dict[str, Any]
    exposure_snapshot: dict[str, Any]
    exposure_as_of: datetime
    created_at: datetime
