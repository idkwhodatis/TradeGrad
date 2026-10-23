"""JSON boundary between the API and the separately locked data runtime.

Dates are exchange-local sessions in [start, end). IDs are registry identities,
not tickers. All evidence is explicit; fixture success is never live evidence.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "marketdata.v1"
TRANSFORM_VERSION = "normalize.v1"
IDENTITY_NAMESPACE = UUID("a311c87f-382a-40ad-aab2-d7db03439322")
Evidence = Literal["documentation", "fixture", "live", "uploaded", "none"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class VendorSymbol(Contract):
    vendor: str
    symbol: str
    valid_from: date | None = None
    valid_to: date | None = None  # exclusive


class Listing(Contract):
    instrument_id: str
    listing_id: str
    symbol: str
    name: str | None = None
    mic: str
    asset_type: Literal["equity", "index", "fund", "contract"] = "equity"
    currency: str
    timezone: str
    calendar: str
    calendar_note: str | None = None
    listed_on: date | None = None
    delisted_on: date | None = None  # exclusive, if authoritatively known
    vendor_symbols: list[VendorSymbol] = Field(default_factory=list)
    identity_status: Literal["registry", "provider_resolved", "user_supplied"] = (
        "provider_resolved"
    )

    @field_validator("instrument_id", "listing_id")
    @classmethod
    def valid_identity(cls, value: str) -> str:
        return str(UUID(value))

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("A valid IANA timezone is required") from exc
        return value

    @field_validator("mic")
    @classmethod
    def valid_mic(cls, value: str) -> str:
        if len(value) != 4 or value.upper() != value:
            raise ValueError("MIC must be four uppercase characters")
        return value

    def vendor_symbol(self, vendor: str, on: date | None = None) -> str:
        matches = [
            m.symbol
            for m in self.vendor_symbols
            if m.vendor == vendor
            and (on is None or m.valid_from is None or m.valid_from <= on)
            and (on is None or m.valid_to is None or on < m.valid_to)
        ]
        if len(set(matches)) != 1:
            raise ValueError(f"missing or ambiguous {vendor} mapping")
        return matches[0]


def make_listing(
    *,
    symbol: str,
    mic: str,
    currency: str,
    timezone: str,
    calendar: str,
    vendor: str,
    name: str | None = None,
    registry_key: str | None = None,
    calendar_note: str | None = None,
    identity_status: Literal[
        "registry", "provider_resolved", "user_supplied"
    ] = "provider_resolved",
) -> Listing:
    """Deterministic provisional identity scoped to venue, never ticker globally.

    A persisted registry must preserve these IDs across vendor-symbol changes.
    Without an authoritative security identifier this does not claim to identify
    a reused symbol across delisting/relisting or cross-venue fungibility.
    """
    key = registry_key or f"{mic}:equity:{symbol}"
    return Listing(
        instrument_id=str(uuid5(IDENTITY_NAMESPACE, f"instrument:{key}")),
        listing_id=str(uuid5(IDENTITY_NAMESPACE, f"listing:{key}:{mic}")),
        symbol=symbol,
        mic=mic,
        name=name,
        currency=currency,
        timezone=timezone,
        calendar=calendar,
        calendar_note=calendar_note,
        identity_status=identity_status,
        vendor_symbols=[VendorSymbol(vendor=vendor, symbol=symbol)],
    )


class WarningRecord(Contract):
    code: str
    message: str
    listing_id: str | None = None
    severity: Literal["info", "warning", "error"] = "warning"


class Capability(Contract):
    feature: str
    status: Literal["documented", "verified", "unsupported", "unknown"]
    connector: str
    provider: str
    endpoint: str
    package_versions: dict[str, str] = Field(default_factory=dict)
    evidence: Evidence = "none"
    symbols: list[str] = Field(default_factory=list)
    start: date | None = None
    end: date | None = None
    checked_at: datetime | None = None
    source_url: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def verified_scope(self):
        if self.status == "verified" and (
            self.evidence != "live"
            or not self.symbols
            or self.checked_at is None
            or not self.package_versions
            or ("bars" in self.feature and (self.start is None or self.end is None))
        ):
            raise ValueError(
                "verification requires live evidence, versions, symbols, dates and checked_at"
            )
        return self


class PriceSemantics(Contract):
    native_mode: str = "unknown"
    split_basis: Literal["unadjusted", "adjusted", "unknown"] = "unknown"
    dividend_basis: Literal["unadjusted", "adjusted", "unknown"] = "unknown"
    adjustment_as_of: datetime | None = None
    algorithm_version: str | None = None
    actions_snapshot_id: str | None = None
    adj_close_split_basis: Literal["adjusted", "unknown"] = "unknown"
    adj_close_dividend_basis: Literal["adjusted", "unknown"] = "unknown"
    note: str | None = None


class RepairSource(Contract):
    """Immutable reference, not a claim of replaying the old upstream bytes."""

    kind: Literal["yahoo_repair_refetch.v1"] = "yahoo_repair_refetch.v1"
    dataset_version_id: UUID
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BarsRequest(Contract):
    listings: list[Listing] = Field(min_length=1, max_length=100)
    start: date
    end: date
    interval: Literal["1d"] = "1d"
    price_mode: str = "provider_native"
    repair_source: RepairSource | None = None
    as_of: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def valid_range(self):
        if (self.price_mode == "repair") != (self.repair_source is not None):
            raise ValueError("Repair requires an explicit immutable parent reference")
        if self.end <= self.start:
            raise ValueError("end must be after start; dates use [start, end)")
        if (self.end - self.start).days > 36525:
            raise ValueError("range exceeds 100 years")
        if self.as_of.tzinfo is None:
            raise ValueError("as_of must include a timezone")
        ids = [listing.listing_id for listing in self.listings]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate listings")
        return self


class Provenance(Contract):
    provenance_id: str
    connector: str
    provider: str
    endpoint: str
    requested_params: dict[str, Any] = Field(default_factory=dict)
    effective_params: dict[str, Any] = Field(default_factory=dict)
    fetched_at: datetime = Field(default_factory=utc_now)
    package_versions: dict[str, str] = Field(default_factory=dict)
    transformation_version: str = TRANSFORM_VERSION
    evidence: Evidence = "none"
    terms_url: str | None = None
    source_url: str | None = None
    note: str | None = None


class RawPayload(Contract):
    listing_id: str | None = None
    provenance_id: str
    format: Literal["dataframe_split", "json", "csv", "parquet"] = "json"
    content: Any
    # Serialized SDK result, not a claim to preserve HTTP wire bytes.
    description: str = "Uncleaned provider SDK return"


class Bar(Contract):
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
    volume_unit: Literal["shares", "lots", "contracts", "unknown"]
    flags: list[str] = Field(default_factory=list)


class CorporateAction(Contract):
    listing_id: str
    session_date: date
    action_type: Literal["dividend", "split", "capital_gain"]
    value: float
    currency: str | None = None
    provenance_id: str
    note: str | None = None


class Coverage(Contract):
    listing_id: str
    requested_start: date
    requested_end: date
    actual_start: date | None = None
    actual_end: date | None = None  # exclusive
    status: Literal["complete", "partial", "failed", "unknown"]
    calendar: str
    calendar_version: str | None = None
    expected_sessions: list[date] = Field(default_factory=list)
    observed_sessions: list[date] = Field(default_factory=list)
    missing_sessions: list[date] = Field(default_factory=list)
    provisional_sessions: list[date] = Field(default_factory=list)
    non_session_dates: list[date] = Field(default_factory=list)
    before_listing_sessions: list[date] = Field(default_factory=list)
    after_delisting_sessions: list[date] = Field(default_factory=list)
    known_suspension_sessions: list[date] = Field(default_factory=list)
    missing_reason: str | None = None
    error_code: str | None = None


class ResolutionResult(Contract):
    schema_version: str = SCHEMA_VERSION
    data: list[Listing] = Field(default_factory=list)
    provenance: list[Provenance] = Field(default_factory=list)
    coverage: list[Coverage] = Field(default_factory=list)
    warnings: list[WarningRecord] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    raw: list[RawPayload] = Field(default_factory=list)


class ProviderResult(Contract):
    schema_version: str = SCHEMA_VERSION
    data: list[Bar] = Field(default_factory=list)
    actions: list[CorporateAction] = Field(default_factory=list)
    raw: list[RawPayload] = Field(default_factory=list)
    provenance: list[Provenance] = Field(default_factory=list)
    coverage: list[Coverage] = Field(default_factory=list)
    warnings: list[WarningRecord] = Field(default_factory=list)
    price_semantics: PriceSemantics = Field(default_factory=PriceSemantics)


class MarketDataProvider(Protocol):
    def capabilities(self) -> list[Capability]: ...
    def resolve_instruments(self, symbols: list[str]) -> ResolutionResult: ...
    def fetch_bars(self, request: BarsRequest) -> ProviderResult: ...


class UniverseMember(Contract):
    listing_id: str
    valid_from: date | None = None
    valid_to: date | None = None
    known_at: datetime | None = None


class UniverseSnapshot(Contract):
    state: Literal[
        "user_fixed",
        "current_snapshot",
        "historical_membership",
        "point_in_time_verified",
    ]
    as_of: datetime
    members: list[UniverseMember]
    provenance: list[Provenance] = Field(default_factory=list)
    warnings: list[WarningRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def enforce_pit(self):
        if self.state == "point_in_time_verified" and (
            not self.provenance
            or any(m.valid_from is None or m.known_at is None for m in self.members)
        ):
            raise ValueError(
                "PIT verification requires membership intervals and knowledge-time evidence"
            )
        return self


class PortableUploadDeclaration(Contract):
    """An explicit research assumption, never independently verified evidence."""

    schema_version: Literal["marketdata.portable-upload-declaration.v1"] = (
        "marketdata.portable-upload-declaration.v1"
    )
    kind: Literal["user_declared_raw_no_actions"]
    acknowledged_unverified: Literal[True]
    symbol: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9.^_-]+$")
    mic: str = Field(pattern=r"^[A-Z0-9]{4}$")
    start: date
    end: date
    source_reference: str = Field(min_length=1, max_length=512)
    evidence_note: str = Field(min_length=1, max_length=2000)

    @field_validator("acknowledged_unverified", mode="before")
    @classmethod
    def explicit_acknowledgement(cls, value):
        if value is not True:
            raise ValueError(
                "Explicit acknowledgement of unverified assumptions is required"
            )
        return value

    @field_validator("source_reference", "evidence_note")
    @classmethod
    def bounded_plain_text(cls, value):
        if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Use nonempty plain text without control characters")
        return value

    @model_validator(mode="after")
    def valid_range(self):
        if self.end <= self.start or (self.end - self.start).days > 3660:
            raise ValueError(
                "Declaration coverage must be nonempty and at most ten years"
            )
        return self


def validate_portable_upload(mapping, *, start=None, end=None):
    declaration = mapping.portable_declaration
    if declaration is None:
        return
    if (
        (declaration.symbol, declaration.mic) != (mapping.symbol, mapping.mic)
        or (start is not None and declaration.start != start)
        or (end is not None and declaration.end != end)
    ):
        raise ValueError(
            "Portable declaration must match the exact upload identity and date range"
        )
    semantics = mapping.price_semantics
    if (
        semantics.native_mode not in {"raw", "user_declared_raw"}
        or semantics.split_basis != "unadjusted"
        or semantics.dividend_basis != "unadjusted"
        or semantics.adjustment_as_of is not None
        or semantics.algorithm_version is not None
        or semantics.actions_snapshot_id is not None
        or mapping.volume_unit != "shares"
        or mapping.currency not in {"USD", "CNY"}
        or getattr(mapping, "unknown_flags", [])
    ):
        raise ValueError(
            "Portable upload requires explicitly raw, unadjusted prices, known share units and no unresolved semantics"
        )


class UploadMapping(Contract):
    symbol: str
    mic: str
    currency: str = "unknown"
    timezone: str
    calendar: str
    columns: dict[str, str]
    date_format: str = "%Y-%m-%d"
    volume_unit: Literal["shares", "lots", "contracts", "unknown"] = "unknown"
    price_semantics: PriceSemantics = Field(default_factory=PriceSemantics)
    portable_declaration: PortableUploadDeclaration | None = None

    @model_validator(mode="after")
    def consistent_venue_metadata(self):
        from .venues import canonical_calendar

        calendar = canonical_calendar(
            self.mic, self.calendar, currency=self.currency, timezone=self.timezone
        )
        object.__setattr__(self, "calendar", calendar)
        validate_portable_upload(self)
        return self


# Generated fixture prices are versioned independently of live transformations.
# A retry must not combine checkpointed bars from different fixture algorithms.
FIXTURE_VERSION = "synthetic-daily.v3"
PORTABLE_FIXTURE_VERSION = "portable.synthetic.daily.v1"


def execution_policy_compatible(
    policy: dict, provider_mode: str, *, provider_name: str | None = None
) -> bool:
    if provider_name == "portable_fixture" and provider_mode != "fixture":
        return False
    fixture = (
        PORTABLE_FIXTURE_VERSION
        if provider_name == "portable_fixture"
        else FIXTURE_VERSION
    )
    return (
        policy.get("schema_version") == SCHEMA_VERSION
        and policy.get("transform_version") == TRANSFORM_VERSION
        and policy.get("worker_contract")
        in {"data-import.v1", "data-import.v2", "data-import.v3"}
        and policy.get("provider_mode") == provider_mode
        and policy.get("fixture_version")
        == (fixture if provider_mode == "fixture" else None)
        and type(policy.get("block_days")) is int
        and 1 <= policy["block_days"] <= 366
    )
