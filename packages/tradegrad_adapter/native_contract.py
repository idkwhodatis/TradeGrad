"""Bounded user declarations, not verified statements about arbitrary Python.

Values are immutable scalar fields on frozen tuple members. Only the isolated
native evaluator exposes a detached parameter map through strategy context.
"""

from __future__ import annotations

import json
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        revalidate_instances="always",
    )

    @field_validator("*", mode="before")
    @classmethod
    def freeze_arrays(cls, value, info):
        if (
            info.field_name in {"choices", "parameters", "required_fields"}
            and type(value) is list
        ):
            return tuple(value)
        return value


Scalar = bool | int | float | str
FieldName = Literal["open", "high", "low", "close", "volume", "amount"]
ParameterName = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")]


class NativeParameter(Contract):
    name: ParameterName
    kind: Literal["integer", "number", "boolean", "string"]
    value: Scalar
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[Scalar, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def bounded_value(self):
        types = {
            "integer": (int,),
            "number": (int, float),
            "boolean": (bool,),
            "string": (str,),
        }
        if type(self.value) not in types[self.kind]:
            raise ValueError(f"Parameter {self.name} must use its declared JSON type")
        values = (self.value, *self.choices)
        if any(type(value) not in types[self.kind] for value in values):
            raise ValueError("Parameter choices must have the declared type")
        if any(
            isinstance(value, str) and (len(value) > 256 or "\x00" in value)
            for value in values
        ):
            raise ValueError(
                "Parameter strings are limited to 256 characters without NUL"
            )
        if any(
            type(value) in (int, float)
            and (abs(value) > 1e12 or not math.isfinite(value))
            for value in values
        ):
            raise ValueError(
                "Parameters require finite numbers with magnitude at most 1e12"
            )
        if self.kind in ("integer", "number"):
            if (
                self.minimum is None
                or self.maximum is None
                or not -1e12 <= self.minimum <= self.maximum <= 1e12
            ):
                raise ValueError(
                    "Numeric parameters require finite ordered minimum and maximum bounds"
                )
            if any(not self.minimum <= value <= self.maximum for value in values):
                raise ValueError(
                    f"Parameter {self.name} or its choices lie outside declared bounds"
                )
        elif self.minimum is not None or self.maximum is not None:
            raise ValueError("Only numeric parameters accept minimum/maximum")
        if self.choices and self.value not in self.choices:
            raise ValueError(f"Parameter {self.name} is not an allowed choice")
        return self


class NativeStrategyContract(Contract):
    contract_version: Literal["native-declarations.v1"] = "native-declarations.v1"
    required_fields: tuple[FieldName, ...] = Field(
        default=("open", "high", "low", "close", "volume"), min_length=1, max_length=6
    )
    lookback_bars: int = Field(ge=0, le=252)
    warmup_bars: int = Field(ge=0, le=252)
    requires_shorting: bool
    parameters: tuple[NativeParameter, ...] = Field(default=(), max_length=32)
    parameter_binding: Literal["context.tradegrad_parameters.v1"] = (
        "context.tradegrad_parameters.v1"
    )
    evidence: Literal["user_declared_not_code_verified"] = (
        "user_declared_not_code_verified"
    )

    @model_validator(mode="after")
    def unique_declarations(self):
        if len(set(self.required_fields)) != len(self.required_fields):
            raise ValueError("Required fields must be unique")
        if len({item.name for item in self.parameters}) != len(self.parameters):
            raise ValueError("Parameter names must be unique")
        if (
            len(
                json.dumps(
                    self.model_dump(mode="json"),
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("utf-8")
            )
            > 16384
        ):
            raise ValueError(
                "Native declarations must fit the 16 KiB evaluator request budget"
            )
        return self

    def parameter_values(self) -> dict[str, Scalar]:
        return {item.name: item.value for item in self.parameters}


def native_context(context: dict, contract: dict | None) -> dict:
    """Used only by the evaluator; None preserves exactly the legacy context."""
    if contract is None:
        return context
    parsed = NativeStrategyContract.model_validate(contract)
    return {**context, "tradegrad_parameters": parsed.parameter_values()}


def validate_native_binding(contract, manifest, parameters):
    expected = contract.model_dump(mode="json") if contract is not None else None
    values = contract.parameter_values() if contract is not None else None
    if expected != manifest or values != parameters:
        raise ValueError(
            "Pin the selected strategy version’s exact declaration and parameter values; create a new immutable version to change them"
        )
