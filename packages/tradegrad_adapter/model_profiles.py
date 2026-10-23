"""Operator-owned model routing. Never resolve secrets in API/configuration code.

The API accepts only the public, content-pinned selection below. Endpoint and
credential-reference configuration belongs to a trusted deployment operator,
not to an HTTP request, model response or evaluator container.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from .config import FrozenModel, ModelConfig, canonical_bytes


class ModelProfileError(ValueError):
    """Safe fixed-message failure, without operator settings or input values."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class OperatorModelProfile(FrozenModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        validate_default=True,
        hide_input_in_errors=True,
    )
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=100)
    provider: Literal["openai", "openrouter", "openai-compatible"] = "openai-compatible"
    model: str = Field(
        min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$"
    )
    base_url: str = Field(min_length=1, max_length=1024, repr=False)
    secret_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]{0,127}$", repr=False)
    owner_ids: tuple[UUID, ...] = Field(min_length=1, max_length=1000, repr=False)
    temperature: float = Field(default=0, ge=0, le=2)
    max_output_tokens: int = Field(default=4096, ge=1, le=16384)
    request_timeout_seconds: float = Field(default=30, ge=1, le=120)
    max_request_bytes: int = Field(default=1048576, ge=1024, le=1048576)
    max_response_bytes: int = Field(default=1048576, ge=1024, le=1048576)
    synthetic: bool = False

    @field_validator("label", "model")
    @classmethod
    def public_text(cls, value):
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Invalid public model profile text")
        if "://" in value or value.lower().startswith(("sk-", "sk_", "bearer", "eyj")):
            raise ValueError(
                "Public model profile text must not contain credentials or URLs"
            )
        return value

    @model_validator(mode="after")
    def endpoint(self):
        try:
            parsed = urlsplit(self.base_url)
            port = parsed.port
            host = parsed.hostname
        except ValueError:
            raise ValueError("Invalid operator model endpoint") from None
        if (
            not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") not in {"/v1", "/api/v1"}
            or any(char.isspace() for char in self.base_url)
            or "\\" in self.base_url
            or "%" in self.base_url
            or len(set(self.owner_ids)) != len(self.owner_ids)
        ):
            raise ValueError("Invalid operator model endpoint or owner allowlist")
        if self.synthetic:
            if parsed.scheme != "http" or host != "127.0.0.1" or not port:
                raise ValueError(
                    "Synthetic model endpoint must use explicit IPv4 loopback and port"
                )
        else:
            if parsed.scheme != "https" or port not in (None, 443):
                raise ValueError("Production model endpoints require HTTPS on port 443")
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise ValueError(
                    "Production model endpoints require an operator-controlled DNS name"
                )
            if (
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
                or "." not in host
                or ".." in host
                or host.endswith(
                    (".localhost", ".local", ".internal", ".test", ".invalid")
                )
            ):
                raise ValueError(
                    "Production model endpoint is not an admitted DNS name"
                )
        return self

    @property
    def sha256(self):
        # Covers routing, secret *reference*, owners and request bounds, never the
        # secret value. Rotation at the same reference is an operator action;
        # model determinism or account identity is not inferred from that value.
        return hashlib.sha256(canonical_bytes(self.model_dump(mode="json"))).hexdigest()

    def public_config(self):
        return ModelConfig(
            provider=self.provider,
            model=self.model,
            profile_id=self.id,
            profile_sha256=self.sha256,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
        )

    def public(self):
        return {
            "id": self.id,
            "label": self.label,
            "config": self.public_config(),
            "synthetic": self.synthetic,
            "request_timeout_seconds": self.request_timeout_seconds,
            "strategy_modes": ["native_akquant", "portable"],
        }


def validate_registry(settings):
    profiles = settings.model_profiles
    if len({profile.id for profile in profiles}) != len(profiles):
        raise ValueError("Operator model profile IDs must be unique")
    if any(profile.id == "fixture" for profile in profiles):
        raise ValueError("The fixture profile ID is reserved")
    if any(profile.synthetic for profile in profiles) and not synthetic_test_enabled(
        settings
    ):
        raise ValueError(
            "Synthetic model profiles require explicit hosted fixture-test mode"
        )


def synthetic_test_enabled(settings):
    return (
        settings.model_synthetic_test_mode
        and settings.data_provider_mode == "fixture"
        and os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("TRADEGRAD_REQUIRE_MODEL_INTEGRATION") == "1"
    )


def available_profiles(settings, owner_id):
    result = [
        {
            "id": "fixture",
            "label": "Deterministic fixture (no network)",
            "config": ModelConfig(),
            "synthetic": True,
            "request_timeout_seconds": 0,
            "strategy_modes": ["native_akquant", "portable"],
        }
    ]
    if settings.model_profiles_enabled and owner_id is not None:
        result.extend(
            profile.public()
            for profile in settings.model_profiles
            if UUID(str(owner_id)) in profile.owner_ids
            and (not profile.synthetic or synthetic_test_enabled(settings))
        )
    return result


def resolve_profile(config: ModelConfig, settings, owner_id):
    if config.provider == "fixture":
        return None
    if not settings.model_profiles_enabled:
        raise ModelProfileError("model_profile_unavailable")
    profile = next(
        (p for p in settings.model_profiles if p.id == config.profile_id), None
    )
    if (
        profile is None
        or UUID(str(owner_id)) not in profile.owner_ids
        or (profile.synthetic and not synthetic_test_enabled(settings))
    ):
        # Unknown and another owner's profile deliberately have the same response.
        raise ModelProfileError("model_profile_unavailable")
    if config != profile.public_config():
        raise ModelProfileError("model_profile_changed")
    return profile
