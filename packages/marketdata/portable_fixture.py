"""Explicitly artificial raw daily equity prices, not captured/live prices.

No corporate actions exist by construction. The independently versioned generator
is only usable as a fixture. It does not relabel Yahoo/AKShare source semantics.
"""

import hashlib
import math
from datetime import date

from .calendars import session_calendar
from .contracts import (
    PORTABLE_FIXTURE_VERSION,
    Bar,
    BarsRequest,
    PriceSemantics,
    Provenance,
    ProviderResult,
    WarningRecord,
)
from .normalize import finish_result

GENERATOR_VERSION = PORTABLE_FIXTURE_VERSION


def portable_fixture_result(request: BarsRequest) -> ProviderResult:
    from .native_research_fixture import (
        research_fixture_result,
        validate_fixture_request,
    )

    if validate_fixture_request(request) is not None:
        return research_fixture_result(request)
    if request.price_mode not in {"raw", "provider_native"}:
        raise ValueError("Portable synthetic fixture requires raw prices")
    if request.start < date(2024, 1, 2) or request.end > date(2025, 1, 1):
        raise ValueError(
            "Portable synthetic fixture covers [2024-01-02,2025-01-01) only"
        )
    sources, bars = [], []
    for listing in request.listings:
        if listing.asset_type != "equity":
            raise ValueError("Portable synthetic fixture supports equities only")
        source_id = hashlib.sha256(
            f"{GENERATOR_VERSION}:{listing.listing_id}:{request.start}:{request.end}".encode()
        ).hexdigest()
        sources.append(
            Provenance(
                provenance_id=source_id,
                connector="portable_fixture",
                provider="synthetic",
                endpoint=GENERATOR_VERSION,
                evidence="fixture",
                requested_params={
                    "listing_id": listing.listing_id,
                    "start": str(request.start),
                    "end": str(request.end),
                },
                effective_params={
                    "is_synthetic": True,
                    "price_basis": "unadjusted",
                    "corporate_actions": "none_by_construction",
                    "generator_version": GENERATOR_VERSION,
                },
                note="Artificial generated prices and zero corporate actions, never live or historical observations.",
            )
        )
        _, sessions = session_calendar(listing, date(2024, 1, 2), date(2025, 1, 1))
        phase = int(hashlib.sha256(listing.listing_id.encode()).hexdigest()[:4], 16) % 7
        for i, session in enumerate(sessions):
            day = session.date()
            if not request.start <= day < request.end:
                continue
            close = round(30 + 0.03 * i + 3 * math.sin((i + phase) / 6), 4)
            opening = round(close - 0.2 * math.sin(i + phase), 4)
            bars.append(
                Bar(
                    listing_id=listing.listing_id,
                    session_date=day,
                    open=opening,
                    high=max(opening, close) + 1,
                    low=min(opening, close) - 1,
                    close=close,
                    volume=100_000 + i,
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
                algorithm_version=GENERATOR_VERSION,
                actions_snapshot_id=f"{GENERATOR_VERSION}:none_by_construction",
                note="Explicit synthetic fixture. No actions exist by generator construction.",
            ),
            warnings=[
                WarningRecord(
                    code="synthetic_fixture_not_live",
                    message="Artificial portable reference fixture, not market observations or live-data evidence.",
                )
            ],
        ),
    )


def action_coverage(request: BarsRequest, result: ProviderResult) -> dict:
    """Unknown is preserved; an empty actions array alone proves nothing."""
    from .native_research_fixture import VERSION, profile_metadata

    version = GENERATOR_VERSION
    metadata = {
        "is_synthetic": True,
        "price_basis": "unadjusted",
        "corporate_actions": "none_by_construction",
        "generator_version": GENERATOR_VERSION,
    }
    if result.price_semantics.algorithm_version == VERSION:
        version, metadata = VERSION, profile_metadata()
    records = []
    for listing in request.listings:
        sources = [
            p
            for p in result.provenance
            if p.connector == "portable_fixture"
            and p.provider == "synthetic"
            and p.endpoint == version
            and p.evidence == "fixture"
            and p.requested_params.get("listing_id") == listing.listing_id
            and p.effective_params == metadata
        ]
        # Blocks may be combined; demand exact gap-free coverage of requested dates.
        intervals = sorted(
            (p.requested_params.get("start"), p.requested_params.get("end"))
            for p in sources
        )
        cursor = str(request.start)
        complete = bool(intervals)
        for start, end in intervals:
            if (
                not isinstance(start, str)
                or not isinstance(end, str)
                or start > cursor
                or end <= start
            ):
                complete = False
                break
            cursor = max(cursor, end)
        complete = (
            complete
            and cursor >= str(request.end)
            and result.price_semantics.algorithm_version == version
            and not result.actions
        )
        records.append(
            {
                "listing_id": listing.listing_id,
                "status": "known_none" if complete else "unknown",
                "provenance_ids": [p.provenance_id for p in sources]
                if complete
                else [],
                "evidence": "synthetic_by_construction" if complete else "none",
            }
        )
    return {
        "schema_version": "marketdata.corporate-action-coverage.v1",
        "start": str(request.start),
        "end": str(request.end),
        "listings": records,
        "is_synthetic": bool(records)
        and all(r["status"] == "known_none" for r in records),
        "generator_version": version
        if records and all(r["status"] == "known_none" for r in records)
        else None,
    }


class PortableFixtureProvider:
    """Fixture-only provider facade for the ordinary resolve/import worker path."""

    def __init__(self, *, mode="fixture"):
        if mode != "fixture":
            raise ValueError(
                "portable_fixture is synthetic and cannot run in live mode"
            )
        self.mode = mode

    def capabilities(self):
        from .contracts import Capability
        from .native_research_fixture import END, START, SYMBOLS, VERSION

        common = {
            "connector": "portable_fixture",
            "provider": "synthetic",
            "endpoint": GENERATOR_VERSION,
            "evidence": "fixture",
            "note": "Artificial generator only; no live market-data or execution certification.",
        }
        return [
            Capability(feature="daily_bars", status="documented", **common),
            Capability(feature="corporate_actions", status="documented", **common),
            Capability(
                feature="point_in_time_universe", status="unsupported", **common
            ),
            Capability(
                feature="native_research_daily_bars",
                status="documented",
                **{
                    **common,
                    "endpoint": VERSION,
                    "symbols": list(SYMBOLS),
                    "start": START,
                    "end": END,
                    "note": "Separate synthetic native research profile; frozen eight-session price cycles, requested user-fixed universe, no portable capacity expansion.",
                },
            ),
        ]

    def resolve_instruments(self, symbols):
        from .contracts import ResolutionResult, make_listing
        from .native_research_fixture import (
            SYMBOLS,
            fixture_profile_for_symbols,
            research_listing,
        )

        fixture_profile_for_symbols(symbols)
        data, unresolved = [], []
        for symbol in symbols:
            if symbol in SYMBOLS:
                data.append(research_listing(symbol))
                continue
            cn = symbol in {"600000", "000001"}
            if not cn and symbol not in {"AAPL", "MSFT"}:
                unresolved.append(symbol)
                continue
            mic = ("XSHG" if symbol == "600000" else "XSHE") if cn else "XNAS"
            data.append(
                make_listing(
                    symbol=symbol,
                    mic=mic,
                    currency="CNY" if cn else "USD",
                    timezone="Asia/Shanghai" if cn else "America/New_York",
                    calendar="XSHG" if cn else "XNYS",
                    vendor="portable_fixture",
                    name=f"Synthetic {symbol} (not market observations)",
                    registry_key=f"{GENERATOR_VERSION}:{mic}:{symbol}",
                    calendar_note=f"{mic} uses declared {'XSHG' if cn else 'XNYS'} regular-equity schedule; proxy venue equivalence is not certified.",
                )
            )
        return ResolutionResult(
            data=data,
            unresolved=unresolved,
            warnings=[
                WarningRecord(
                    code="synthetic_fixture_not_live",
                    message="Nominal synthetic instruments, not authoritative live instrument resolution.",
                )
            ],
        )

    def fetch_bars(self, request):
        return portable_fixture_result(request)
