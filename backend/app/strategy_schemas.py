"""Bounded native strategy submissions. Source is never normalized or evaluated."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.app.schemas import Task

MAX_STRATEGY_BYTES = 128 * 1024


class StrategyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=255)
    task: Task
    code: str = Field(
        min_length=1,
        max_length=MAX_STRATEGY_BYTES,
        description="Exact Python source, at most 131072 UTF-8 bytes. Syntax parsing only; never executed by the API.",
    )
    parent_id: UUID | None = None

    @field_validator("name")
    @classmethod
    def visible_name(cls, value: str) -> str:
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise ValueError("Name must be valid UTF-8 text") from None
        if not value.strip() or any(
            ord(char) < 32 or ord(char) == 127 for char in value
        ):
            raise ValueError("Name must contain visible text and no control characters")
        return value

    @field_validator("code")
    @classmethod
    def bounded_utf8(cls, value: str) -> str:
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise ValueError("Source must be valid UTF-8 text") from None
        if len(encoded) > MAX_STRATEGY_BYTES:
            raise ValueError("Source must not exceed 131072 UTF-8 bytes")
        if not value.strip() or "\x00" in value:
            raise ValueError("Source must contain text and no NUL bytes")
        return value
