"""Bounded, explicitly synthetic native cross-sectional research prices.

The original eight-session hand-worked price/volume sample is repeated over a
frozen 41-session window. This is not a market history or a portable-engine
capacity change. Imports use their requested user-fixed universe; the sample's
separate dynamic membership example is deliberately NOT imported.
"""

from __future__ import annotations

import hashlib
from datetime import date

from packages.research_trace.samples import CROSS_SECTION_TAPE

from .contracts import (
    NATIVE_RESEARCH_FIXTURE_PROFILE,
    NATIVE_RESEARCH_FIXTURE_VERSION,
    Bar,
    BarsRequest,
    Listing,
    PriceSemantics,
    Provenance,
    ProviderResult,
    WarningRecord,
    make_listing,
)
from .normalize import canonical_json, finish_result

PROFILE = NATIVE_RESEARCH_FIXTURE_PROFILE
VERSION = NATIVE_RESEARCH_FIXTURE_VERSION
START, END = date(2024, 1, 2), date(2024, 3, 1)
SYMBOLS = ("A", "B", "C", "D")
SAMPLE_PRICES_SHA256 = (
    "a3b697990b5b43d131e0c9373750583d342870e2c29cec3a382bb9ff8d3f13fa"
)
# Literal exchange sessions freeze cycle offsets independently of request blocks.
SESSION_DATES = tuple(
    date.fromisoformat(value)
    for value in (
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
        "2024-01-05",
        "2024-01-08",
        "2024-01-09",
        "2024-01-10",
        "2024-01-11",
        "2024-01-12",
        "2024-01-16",
        "2024-01-17",
        "2024-01-18",
        "2024-01-19",
        "2024-01-22",
        "2024-01-23",
        "2024-01-24",
        "2024-01-25",
        "2024-01-26",
        "2024-01-29",
        "2024-01-30",
        "2024-01-31",
        "2024-02-01",
        "2024-02-02",
        "2024-02-05",
        "2024-02-06",
        "2024-02-07",
        "2024-02-08",
        "2024-02-09",
        "2024-02-12",
        "2024-02-13",
        "2024-02-14",
        "2024-02-15",
        "2024-02-16",
        "2024-02-20",
        "2024-02-21",
        "2024-02-22",
        "2024-02-23",
        "2024-02-26",
        "2024-02-27",
        "2024-02-28",
        "2024-02-29",
    )
)


def fixture_profile_for_symbols(symbols: list[str]) -> str | None:
    """Choose only the disjoint, explicitly named research fixture symbols."""
    selected = set(symbols)
    if selected.intersection(SYMBOLS):
        if not selected.issubset(SYMBOLS):
            raise ValueError(
                "Native research fixture cannot mix with legacy or other symbols"
            )
        return PROFILE
    return None


def research_listing(symbol: str) -> Listing:
    if symbol not in SYMBOLS:
        raise ValueError("Unknown native research fixture symbol")
    return make_listing(
        symbol=symbol,
        mic="XNYS",
        currency="USD",
        timezone="America/New_York",
        calendar="XNYS",
        vendor="portable_fixture",
        name=f"Synthetic controlled research {symbol} (not market observations)",
        registry_key=f"{VERSION}:XNYS:{symbol}",
        calendar_note="Frozen synthetic native research profile on XNYS sessions; not a real security.",
    )


def fixture_profile_for_listings(listings: list[Listing]) -> str | None:
    profile = fixture_profile_for_symbols([listing.symbol for listing in listings])
    if profile is not None:
        for listing in listings:
            if listing != research_listing(listing.symbol):
                raise ValueError(
                    "Native research fixture listing identity or metadata disagrees"
                )
    return profile


def validate_fixture_request(request: BarsRequest) -> str | None:
    profile = fixture_profile_for_listings(request.listings)
    if profile is not None:
        if request.price_mode not in {"raw", "provider_native"}:
            raise ValueError("Native research synthetic fixture requires raw prices")
        if request.start < START or request.end > END:
            raise ValueError(
                "Native research synthetic fixture covers [2024-01-02,2024-03-01) only"
            )
    return profile


def profile_metadata() -> dict:
    return {
        "is_synthetic": True,
        "price_basis": "unadjusted",
        "corporate_actions": "none_by_construction",
        "generator_version": VERSION,
        "fixture_profile": PROFILE,
        "source_sample_id": CROSS_SECTION_TAPE.sample_id,
        "sample_prices_sha256": SAMPLE_PRICES_SHA256,
        "cycle_sessions": 8,
        "fixture_start": str(START),
        "fixture_end": str(END),
        "universe_semantics": "requested_user_fixed_not_sample_membership",
    }


def _validate_frozen_sample() -> None:
    value = {
        "sample_id": CROSS_SECTION_TAPE.sample_id,
        "dates": CROSS_SECTION_TAPE.dates,
        "instruments": [
            {
                "symbol": item.instrument_id,
                "closes": item.closes,
                "volumes": item.volumes,
            }
            for item in CROSS_SECTION_TAPE.instruments
        ],
    }
    if hashlib.sha256(canonical_json(value)).hexdigest() != SAMPLE_PRICES_SHA256:
        raise ValueError(
            "Frozen native research sample changed; review and version the profile"
        )


def research_fixture_result(request: BarsRequest) -> ProviderResult:
    if validate_fixture_request(request) != PROFILE:
        raise ValueError("Native research fixture requires its own profile listings")
    _validate_frozen_sample()
    instruments = {item.instrument_id: item for item in CROSS_SECTION_TAPE.instruments}
    sources, bars = [], []
    for listing in request.listings:
        item = instruments[listing.symbol]
        sources.append(
            Provenance(
                provenance_id=hashlib.sha256(
                    f"{VERSION}:{listing.listing_id}:{request.start}:{request.end}".encode()
                ).hexdigest(),
                connector="portable_fixture",
                provider="synthetic",
                endpoint=VERSION,
                evidence="fixture",
                requested_params={
                    "listing_id": listing.listing_id,
                    "start": str(request.start),
                    "end": str(request.end),
                },
                effective_params=profile_metadata(),
                note="Repeated frozen hand-worked prices and volumes, never historical observations. No sample dynamic membership is imported.",
            )
        )
        for index, day in enumerate(SESSION_DATES):
            if not request.start <= day < request.end:
                continue
            offset = index % 8
            close = item.closes[offset]
            bars.append(
                Bar(
                    listing_id=listing.listing_id,
                    session_date=day,
                    open=close,
                    high=close + 0.1,
                    low=close - 0.1,
                    close=close,
                    volume=item.volumes[offset],
                    currency=listing.currency,
                    volume_unit="shares",
                )
            )
    return finish_result(
        request,
        ProviderResult(
            data=bars,
            provenance=sources,
            price_semantics=PriceSemantics(
                native_mode="synthetic_raw",
                split_basis="unadjusted",
                dividend_basis="unadjusted",
                algorithm_version=VERSION,
                actions_snapshot_id=f"{VERSION}:none_by_construction",
                note="Explicit synthetic native research profile; no corporate actions by construction.",
            ),
            warnings=[
                WarningRecord(
                    code="synthetic_fixture_not_live",
                    message="Artificial native research prices, not market observations, live-data evidence or portable-capacity certification.",
                )
            ],
        ),
    )


def validate_research_bundle(request: BarsRequest, result: ProviderResult) -> bool:
    """Fail closed after singleton worker blocks combine, before any file write."""
    profile_ids = {research_listing(symbol).listing_id for symbol in SYMBOLS}
    relevant = (
        any(
            listing.listing_id in profile_ids
            or (
                listing.symbol in SYMBOLS
                and any(
                    mapping.vendor == "portable_fixture"
                    for mapping in listing.vendor_symbols
                )
            )
            for listing in request.listings
        )
        or any(
            source.endpoint == VERSION
            or source.effective_params.get("fixture_profile") == PROFILE
            for source in result.provenance
        )
        or result.price_semantics.algorithm_version == VERSION
    )
    if not relevant:
        return False
    if validate_fixture_request(request) != PROFILE:
        raise ValueError("Native research fixture bundle requires profile listings")
    if (
        result.price_semantics.algorithm_version != VERSION
        or result.price_semantics.native_mode != "synthetic_raw"
        or result.price_semantics.split_basis != "unadjusted"
        or result.price_semantics.dividend_basis != "unadjusted"
        or result.price_semantics.actions_snapshot_id
        != f"{VERSION}:none_by_construction"
        or result.actions
        or not result.provenance
        or any(
            source.connector != "portable_fixture"
            or source.provider != "synthetic"
            or source.endpoint != VERSION
            or source.evidence != "fixture"
            or source.effective_params != profile_metadata()
            for source in result.provenance
        )
    ):
        raise ValueError(
            "Native research fixture cannot mix provenance or price semantics"
        )
    intervals = {listing.listing_id: [] for listing in request.listings}
    for source in result.provenance:
        params = source.requested_params
        listing_id = params.get("listing_id")
        try:
            start = date.fromisoformat(params["start"])
            end = date.fromisoformat(params["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "Native research fixture provenance interval is invalid"
            ) from exc
        expected_id = hashlib.sha256(
            f"{VERSION}:{listing_id}:{start}:{end}".encode()
        ).hexdigest()
        if (
            not isinstance(listing_id, str)
            or listing_id not in intervals
            or not START <= start < end <= END
            or source.provenance_id != expected_id
        ):
            raise ValueError("Native research fixture provenance identity is invalid")
        intervals[listing_id].append((start, end))
    for blocks in intervals.values():
        cursor = request.start
        for start, end in sorted(blocks):
            if start > cursor:
                break
            cursor = max(cursor, end)
        if cursor < request.end:
            raise ValueError(
                "Native research fixture provenance coverage is incomplete"
            )
    return True
