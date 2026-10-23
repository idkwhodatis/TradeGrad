"""Immutable primitive types for the first-party portable contract."""

import re
from datetime import UTC, date, datetime
from typing import Annotated, Literal, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        revalidate_instances="always",
    )

    @model_validator(mode="before")
    @classmethod
    def literal_types_are_strict(cls, value):
        # Pydantic's Literal[False]/Literal[1] otherwise accept 0/True/1.0
        # even with strict=True. Do not silently reinterpret wire capabilities.
        if isinstance(value, dict):
            value = dict(value)
            for name, field in cls.model_fields.items():
                # A model-before validator materializes JSON arrays as Python
                # lists; explicitly freeze these before strict tuple validation.
                if (
                    name in value
                    and get_origin(field.annotation) is tuple
                    and type(value[name]) is list
                ):
                    value[name] = tuple(value[name])
                if name in value and get_origin(field.annotation) is Literal:
                    types = {type(item) for item in get_args(field.annotation)}
                    if type(value[name]) not in types:
                        raise ValueError(
                            f"literal field requires exact JSON type: {name}"
                        )
        return value


def utc_timestamp(value):
    if isinstance(value, str):
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value
        ):
            raise ValueError("timestamp must be explicit UTC RFC3339 ending in Z")
        value = datetime.fromisoformat(value)
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("timezone-aware datetime required")
    return value.astimezone(UTC)


def session_date(value):
    if type(value) is str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("session must be YYYY-MM-DD")
        return date.fromisoformat(value)
    if type(value) is not date:
        raise ValueError("session must be a date, not a timestamp")
    return value


def canonical_identity(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError("identity must be a canonical lowercase UUID, never a ticker")
    return value


Timestamp = Annotated[datetime, BeforeValidator(utc_timestamp)]
SessionDate = Annotated[date, BeforeValidator(session_date)]
Identity = Annotated[str, BeforeValidator(canonical_identity)]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Name = Annotated[
    str, Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
NonNegative = Annotated[float, Field(ge=0, le=1e15)]
Positive = Annotated[float, Field(gt=0, le=1e15)]
