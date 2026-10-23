"""SDK-free public connection snapshots. No credentials or configurable endpoints.

A revision identifies owner-local preferences, not connectivity or data rights.
Existing provider provenance and its hashes deliberately remain unchanged.
"""

import hashlib
import json
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

PUBLIC_PROVIDERS = ("akshare", "yfinance", "openbb")
DEFAULT_MODES = {
    "akshare": ("provider_native", "raw", "qfq", "hfq"),
    # Repair needs an immutable parent; it cannot be a generic default.
    "yfinance": ("provider_native",),
    "openbb": ("provider_native",),
}


def connection_identity(owner_id, provider):
    return uuid5(NAMESPACE_URL, f"tradegrad:provider-connection:{owner_id}:{provider}")


def connection_hash(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


class ConnectionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["provider-connection.v1"] = "provider-connection.v1"
    connection_id: UUID
    provider: Literal["akshare", "yfinance", "openbb"]
    revision: int = Field(ge=0)
    enabled: bool
    is_default: bool
    default_price_mode: Literal["provider_native", "raw", "qfq", "hfq"]
    auth_mode: Literal["public_no_auth"] = "public_no_auth"
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_config(self):
        if self.is_default and not self.enabled:
            raise ValueError("A disabled connection cannot be the market default")
        if self.default_price_mode not in DEFAULT_MODES[self.provider]:
            raise ValueError("Unsupported default price mode for this public adapter")
        if self.config_sha256 != connection_hash(
            self.model_dump(mode="json", exclude={"config_sha256"})
        ):
            raise ValueError("Provider connection configuration hash does not match")
        return self


def make_snapshot(
    owner_id,
    provider,
    revision=0,
    *,
    enabled=True,
    is_default=None,
    default_price_mode="provider_native",
):
    value = {
        "schema_version": "provider-connection.v1",
        "connection_id": str(connection_identity(owner_id, provider)),
        "provider": provider,
        "revision": revision,
        "enabled": enabled,
        "is_default": provider != "openbb" if is_default is None else is_default,
        "default_price_mode": default_price_mode,
        "auth_mode": "public_no_auth",
    }
    return ConnectionSnapshot(**value, config_sha256=connection_hash(value))


def validate_request_connection(request, provider, owner_id):
    """Fail closed before SDK/checkpoint access, without reading mutable preferences.

    v1/v2 jobs have no pin and retain their original hashes and behavior. Only v3
    may contain a connection; a pin cannot be stripped or downgraded silently.
    """
    version = request.get("_execution", {}).get("worker_contract")
    snapshot = request.get("_provider_connection")
    if version != "data-import.v3":
        if snapshot is not None:
            raise ValueError("A connection pin requires the v3 worker contract")
        return None
    value = ConnectionSnapshot.model_validate(snapshot)
    if (
        value.revision < 1
        or not value.enabled
        or value.provider != provider
        or value.connection_id != connection_identity(owner_id, provider)
    ):
        raise ValueError(
            "Provider connection is not a persisted owner/provider binding"
        )
    return value
