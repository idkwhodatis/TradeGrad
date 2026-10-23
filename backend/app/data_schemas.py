"""SDK-free, strict public M2 contracts. Test/provider secrets are never request fields."""

import json
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from backend.app.schemas import PublicModel
from packages.marketdata.contracts import (
    Capability,
    Listing,
    PriceSemantics,
    UniverseMember,
)

ProviderName = Literal["akshare", "yfinance", "openbb", "portable_fixture"]
ImportStatus = Literal[
    "requested",
    "fetching",
    "normalizing",
    "validating",
    "ready",
    "partial",
    "failed",
    "cancelled",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderPublic(StrictModel):
    id: ProviderName
    name: str
    upstream: str
    markets: list[str]
    price_modes: list[str]
    capabilities: list[Capability]
    runtime_mode: Literal["live", "fixture"]
    fixture_notice: str | None = None


class ResolveCreate(StrictModel):
    provider: ProviderName
    symbols: list[str] = Field(min_length=1, max_length=100)
    market: Literal["CN", "US"]


class ImportCreate(StrictModel):
    provider: ProviderName
    name: str = Field(default="Market data snapshot", min_length=1, max_length=255)
    market: Literal["CN", "US"]
    symbols: list[str] = Field(default_factory=list, max_length=100)
    listing_ids: list[UUID] = Field(default_factory=list, max_length=100)
    start_date: date
    end_date: date = Field(description="Exclusive end date [start_date,end_date)")
    price_mode: Literal["provider_native", "raw", "qfq", "hfq", "repair"] = (
        "provider_native"
    )
    universe_version_id: UUID | None = None

    @model_validator(mode="after")
    def validate_request(self):
        if bool(self.symbols) == bool(self.listing_ids):
            raise ValueError("Supply exactly one of symbols or resolved listing_ids")
        if (
            self.start_date >= self.end_date
            or (self.end_date - self.start_date).days > 3660
        ):
            raise ValueError("Date range must be nonempty and at most ten years")
        if len(set(self.symbols)) != len(self.symbols) or len(
            set(self.listing_ids)
        ) != len(self.listing_ids):
            raise ValueError("Duplicate securities are not allowed")
        return self


class BlockPublic(PublicModel):
    id: UUID
    symbol: str
    listing_id: UUID | None
    start_date: date
    end_date: date
    status: str
    attempts: int
    content_sha256: str | None
    coverage: list[dict[str, Any]]
    error: str | None
    updated_at: datetime


class ImportPublic(PublicModel):
    id: UUID
    kind: Literal["resolve", "import", "upload"]
    provider: str
    status: ImportStatus
    generation: int
    request: dict[str, Any]
    listing_ids: list[str]
    progress: dict[str, Any]
    warnings: list[dict[str, Any]]
    error: str | None
    dataset_version_id: UUID | None
    parent_version_id: UUID | None
    cancel_requested: bool
    created_at: datetime
    updated_at: datetime

    @field_serializer("request")
    def public_request(self, value):
        public = dict(value)
        if "upload" in public:
            public["upload"] = {
                k: v for k, v in public["upload"].items() if k != "storage_key"
            }
        return public


class ImportDetail(ImportPublic):
    blocks: list[BlockPublic]
    blocks_total: int
    blocks_offset: int
    blocks_limit: int


class ListingPublic(Listing):
    pass


class UniverseCreate(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    state: Literal[
        "user_fixed",
        "current_snapshot",
        "historical_membership",
        "point_in_time_verified",
    ] = "user_fixed"
    as_of: datetime
    members: list[UniverseMember] = Field(min_length=1, max_length=100)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def evidence_rules(self):
        if self.as_of.tzinfo is None:
            raise ValueError("as_of requires timezone")
        if len({m.listing_id for m in self.members}) != len(self.members):
            raise ValueError("Duplicate members")
        for record in self.evidence:
            try:
                bounded = len(json.dumps(record, allow_nan=False).encode()) <= 16_384
            except (ValueError, TypeError):
                bounded = False
            if not bounded or not any(
                isinstance(record.get(key), str) and record[key].strip()
                for key in ("source", "reference", "url", "document")
            ):
                raise ValueError("Evidence requires a bounded source/reference record")
        if self.state == "current_snapshot" and not self.evidence:
            raise ValueError(
                "Current snapshots require source evidence; user evidence remains unverified"
            )
        if self.state in {"historical_membership", "point_in_time_verified"} and (
            not self.evidence or any(m.valid_from is None for m in self.members)
        ):
            raise ValueError(
                "Historical membership requires evidence and validity intervals"
            )
        if self.state == "point_in_time_verified":
            # V1 cannot independently verify arbitrary client-supplied evidence.
            raise ValueError(
                "PIT verification is not available for user-submitted evidence; use historical_membership"
            )
        for m in self.members:
            if m.valid_to is not None and (
                m.valid_from is None or m.valid_to <= m.valid_from
            ):
                raise ValueError("Membership validity intervals must be nonempty")
        return self


class UniversePublic(PublicModel):
    id: UUID
    name: str
    state: str
    as_of: datetime
    members: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    content_sha256: str
    created_at: datetime


class UploadMapping(StrictModel):
    name: str = Field(default="Uploaded data snapshot", min_length=1, max_length=255)
    symbol: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9.^_-]+$")
    market: Literal["CN", "US"]
    mic: str = Field(min_length=4, max_length=4, pattern=r"^[A-Z0-9]{4}$")
    currency: str = Field(pattern=r"^(?:[A-Z]{3}|unknown)$")
    timezone: str
    calendar: str
    start_date: date
    end_date: date
    columns: dict[str, str]
    date_format: str = "%Y-%m-%d"
    volume_unit: Literal["shares", "lots", "contracts", "unknown"]
    price_semantics: PriceSemantics
    unknown_flags: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def mapping_valid(self):
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(self.timezone)
        except (KeyError, ValueError):
            raise ValueError("A valid IANA timezone is required") from None
        cn = self.mic in {"XSHG", "XSHE"}
        us = self.mic in {"XNAS", "XNYS", "XASE", "ARCX", "BATS", "IEXG"}
        if (self.market == "CN" and not cn) or (self.market == "US" and not us):
            raise ValueError("Upload MIC must belong to the selected supported market")
        expected_currency = "CNY" if cn else "USD"
        expected_timezone = "Asia/Shanghai" if cn else "America/New_York"
        if (
            self.currency not in {expected_currency, "unknown"}
            or self.timezone != expected_timezone
        ):
            raise ValueError(
                "Upload currency and timezone must match the selected listing venue"
            )
        from packages.marketdata.venues import canonical_calendar

        self.calendar = canonical_calendar(
            self.mic, self.calendar, currency=self.currency, timezone=self.timezone
        )
        required = {"session_date", "open", "high", "low", "close", "volume"}
        if not required <= self.columns.keys() or set(self.columns) - required - {
            "amount",
            "adj_close",
        }:
            raise ValueError(
                "Map session_date, open, high, low, close, volume and optionally amount/adj_close"
            )
        if len(set(self.columns.values())) != len(self.columns):
            raise ValueError("Each normalized field requires a distinct input column")
        if (
            self.start_date >= self.end_date
            or (self.end_date - self.start_date).days > 3660
        ):
            raise ValueError("Date range must be nonempty and at most ten years")
        if self.currency == "unknown" and "currency_unknown" not in self.unknown_flags:
            raise ValueError("Unknown currency must be explicitly flagged")
        if (
            self.volume_unit == "unknown"
            and "volume_unit_unknown" not in self.unknown_flags
        ):
            raise ValueError("Unknown volume units must be explicitly flagged")
        return self


class PreviewBar(StrictModel):
    listing_id: str
    session_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    adj_close: float | None = None
    amount: float | None = None
    currency: str
    volume_unit: str
    flags: list[str]


class PreviewPage(StrictModel):
    data: list[PreviewBar]
    count: int
    offset: int
    limit: int
    listing_id: str


class CoveragePublic(StrictModel):
    dataset_version_id: UUID
    coverage: list[dict[str, Any]]
    quality: dict[str, Any]
    provenance: list[dict[str, Any]]
    universe: dict[str, Any]
