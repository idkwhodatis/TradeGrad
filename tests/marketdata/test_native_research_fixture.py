"""Frozen local fixture/bundle contracts, not live or browser certification."""

import hashlib
import json
import subprocess
import sys
from datetime import UTC, date, datetime

import pytest

from packages.marketdata.calendars import session_calendar
from packages.marketdata.contracts import (
    PORTABLE_FIXTURE_VERSION,
    BarsRequest,
    ProviderResult,
    make_listing,
)
from packages.marketdata.native_research_fixture import (
    END,
    PROFILE,
    SAMPLE_PRICES_SHA256,
    SESSION_DATES,
    START,
    SYMBOLS,
    VERSION,
    fixture_profile_for_listings,
    fixture_profile_for_symbols,
    profile_metadata,
    validate_fixture_request,
)
from packages.marketdata.normalize import canonical_json, finish_result
from packages.marketdata.portable_fixture import (
    GENERATOR_VERSION,
    PortableFixtureProvider,
    action_coverage,
)
from packages.marketdata.storage import verify_bundle, write_bundle
from packages.research_trace.samples import CROSS_SECTION_TAPE


def request(symbols=SYMBOLS, start=START, end=END):
    return BarsRequest(
        listings=PortableFixtureProvider().resolve_instruments(list(symbols)).data,
        start=start,
        end=end,
        as_of=datetime(2025, 1, 2, tzinfo=UTC),
        price_mode="raw",
    )


@pytest.mark.parametrize(
    "symbol,count,digest",
    [
        (
            "AAPL",
            252,
            "c801258d879da51fced06e1f13ad20feb99a3cbc05c8cec0fe1aad6a77065898",
        ),
        (
            "MSFT",
            252,
            "a64232ddc6c73378942f0bcea92a36f28297e4eb2f2090b9aa75f424730d854d",
        ),
        (
            "600000",
            242,
            "baec7ef23ef9fb8ac5d11359491699b0110ce89215eeb134c6cc43956d4923f3",
        ),
        (
            "000001",
            242,
            "e2ee834dab55c02cbbbdbb7287e5feb43597bfe74ec5368f539f12a8537f9bf2",
        ),
    ],
)
def test_entire_legacy_year_and_listing_semantics_are_golden_unchanged(
    symbol, count, digest
):
    req = request([symbol], end=date(2025, 1, 1))
    result = PortableFixtureProvider().fetch_bars(req)
    assert (
        GENERATOR_VERSION == PORTABLE_FIXTURE_VERSION == "portable.synthetic.daily.v1"
    )
    assert len(result.data) == count
    value = {
        "listing": req.listings[0].model_dump(mode="json"),
        "data": [bar.model_dump(mode="json") for bar in result.data],
        "price_semantics": result.price_semantics.model_dump(mode="json"),
        "provenance": [
            item.model_dump(mode="json", exclude={"fetched_at"})
            for item in result.provenance
        ],
    }
    assert hashlib.sha256(canonical_json(value)).hexdigest() == digest


def test_first_eight_sessions_equal_literal_manual_tape_and_repeat_absolute_offsets():
    req = request()
    result = PortableFixtureProvider().fetch_bars(req)
    assert len(result.data) == 164
    assert len(SESSION_DATES) == 41
    assert (
        tuple(day.isoformat() for day in SESSION_DATES[:8]) == CROSS_SECTION_TAPE.dates
    )
    assert (
        tuple(day.date() for day in session_calendar(req.listings[0], START, END)[1])
        == SESSION_DATES
    )
    assert date(2024, 1, 15) not in SESSION_DATES
    assert date(2024, 2, 19) not in SESSION_DATES
    expected = {
        "A": ((10, 10.5, 11, 11.5, 12, 12.5, 13, 12), 1000),
        "B": ((10, 11, 12, 13, 12, 11, 10, 11), 1000),
        "C": ((10, 12, 15, 16, 15, 14, 13, 12), 10),
        "D": ((10, 10.2, 10.5, 10.5, 10.5, 10.5, 10.5, 10.5), 1000),
    }
    for listing in req.listings:
        bars = [bar for bar in result.data if bar.listing_id == listing.listing_id]
        closes, volume = expected[listing.symbol]
        assert [bar.session_date for bar in bars] == list(SESSION_DATES)
        for index, bar in enumerate(bars):
            close = closes[index % 8]
            assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (
                close,
                close + 0.1,
                close - 0.1,
                close,
                volume,
            )
    assert all(coverage.status == "complete" for coverage in result.coverage)


def test_one_four_listing_bundle_has_real_expected_aliases_and_explicit_profile(
    tmp_path,
):
    req = request()
    result = PortableFixtureProvider().fetch_bars(req)
    manifest = write_bundle(tmp_path / "basket", req, result)
    verify_bundle(tmp_path / "basket", manifest)
    assert manifest["ready"] and manifest["row_count"] == 164
    assert manifest["is_synthetic"] is True
    assert manifest["data_evidence"] == "synthetic_fixture_not_live"
    assert manifest["fixture_profile"] == profile_metadata()
    assert manifest["fixture_profile"]["sample_prices_sha256"] == SAMPLE_PRICES_SHA256
    assert set(manifest["evaluator_symbol_mapping"].values()) == set(SYMBOLS)
    assert set(manifest["files"]) == {
        "data/A.csv",
        "data/B.csv",
        "data/C.csv",
        "data/D.csv",
        "universe/fixed_list_2024-01.csv",
    }
    assert manifest["universe"]["state"] == "user_fixed"
    assert manifest["universe"]["point_in_time"] is False
    universe = (tmp_path / "basket/universe/fixed_list_2024-01.csv").read_text()
    assert len(universe.splitlines()) == 5
    assert "2024-01-09" not in universe  # Does not import the manual tape's B exit.
    assert all(
        listing.vendor_symbol("portable_fixture") == listing.symbol
        for listing in req.listings
    )
    assert all(listing.mic == listing.calendar == "XNYS" for listing in req.listings)
    coverage = json.loads(
        (tmp_path / "basket/corporate-action-coverage.json").read_bytes()
    )
    assert coverage["generator_version"] == VERSION != GENERATOR_VERSION
    assert coverage["is_synthetic"] is True
    assert all(row["status"] == "known_none" for row in coverage["listings"])
    assert all(source.endpoint == VERSION for source in result.provenance)
    assert all(source.evidence == "fixture" for source in result.provenance)


def test_single_listing_date_blocks_recombine_without_request_relative_cycle_shift(
    tmp_path,
):
    req = request()
    provider = PortableFixtureProvider()
    full = provider.fetch_bars(req)
    pieces = [
        provider.fetch_bars(request([symbol], start=start, end=end))
        for symbol in SYMBOLS
        for start, end in [(START, date(2024, 1, 17)), (date(2024, 1, 17), END)]
    ]
    combined = finish_result(
        req,
        ProviderResult(
            data=[bar for part in pieces for bar in part.data],
            provenance=[source for part in pieces for source in part.provenance],
            price_semantics=pieces[0].price_semantics,
        ),
    )
    assert combined.data == full.data
    assert write_bundle(tmp_path / "blocks", req, combined)["ready"]
    coverage = action_coverage(req, combined)
    assert coverage["is_synthetic"] is True
    assert all(len(row["provenance_ids"]) == 2 for row in coverage["listings"])


@pytest.mark.parametrize("symbols", [["A", "AAPL"], ["B", "600000"], ["C", "UNKNOWN"]])
def test_profile_cannot_mix_at_resolution_or_fetch(symbols):
    with pytest.raises(ValueError, match="mix"):
        PortableFixtureProvider().resolve_instruments(symbols)
    with pytest.raises(ValueError, match="mix"):
        fixture_profile_for_symbols(symbols)
    listings = [
        PortableFixtureProvider().resolve_instruments([symbol]).data[0]
        for symbol in ["A", "AAPL"]
    ]
    with pytest.raises(ValueError, match="mix"):
        PortableFixtureProvider().fetch_bars(
            request().model_copy(update={"listings": listings})
        )


@pytest.mark.parametrize(
    "change,match",
    [
        ({"start": date(2024, 1, 1)}, "covers"),
        ({"end": date(2024, 3, 2)}, "covers"),
        ({"price_mode": "qfq"}, "raw"),
    ],
)
def test_profile_bounds_do_not_inherit_legacy_year(change, match):
    with pytest.raises(ValueError, match=match):
        PortableFixtureProvider().fetch_bars(request().model_copy(update=change))


@pytest.mark.parametrize(
    "field,value",
    [
        ("mic", "XNAS"),
        ("currency", "CNY"),
        ("calendar", "XNAS"),
        ("name", "Real security"),
    ],
)
def test_native_listing_metadata_cannot_be_forged(field, value):
    listing = request(["A"]).listings[0].model_copy(update={field: value})
    with pytest.raises(ValueError, match="identity or metadata"):
        fixture_profile_for_listings([listing])


@pytest.mark.parametrize("mix_kind", ["listings", "provenance", "semantics"])
def test_combined_worker_publication_cannot_silently_mix_profiles(tmp_path, mix_kind):
    req = request()
    provider = PortableFixtureProvider()
    result = provider.fetch_bars(req)
    legacy_request = request(["AAPL"])
    legacy = provider.fetch_bars(legacy_request)
    if mix_kind == "listings":
        req = req.model_copy(
            update={"listings": req.listings + legacy_request.listings}
        )
        result = result.model_copy(update={"data": result.data + legacy.data})
    elif mix_kind == "provenance":
        result = result.model_copy(
            update={"provenance": result.provenance + legacy.provenance}
        )
    else:
        result = result.model_copy(update={"price_semantics": legacy.price_semantics})
    directory = tmp_path / "must_not_publish"
    with pytest.raises(ValueError, match="mix"):
        write_bundle(directory, req, result)
    assert not directory.exists()


def test_profile_metadata_is_not_sufficient_to_invent_action_evidence():
    req = request()
    result = PortableFixtureProvider().fetch_bars(req)
    missing = result.model_copy(update={"provenance": result.provenance[1:]})
    assert action_coverage(req, missing)["is_synthetic"] is False
    corrupted = result.model_copy(
        update={
            "provenance": [
                source.model_copy(update={"effective_params": {}})
                for source in result.provenance
            ]
        }
    )
    assert all(
        row["status"] == "unknown"
        for row in action_coverage(req, corrupted)["listings"]
    )


@pytest.mark.parametrize(
    "corruption", ["missing_listing", "invalid_id", "invalid_interval"]
)
def test_bundle_requires_coherent_gap_free_profile_provenance(tmp_path, corruption):
    req = request()
    result = PortableFixtureProvider().fetch_bars(req)
    sources = list(result.provenance)
    if corruption == "missing_listing":
        sources = sources[1:]
    elif corruption == "invalid_id":
        sources[0] = sources[0].model_copy(update={"provenance_id": "f" * 64})
    else:
        sources[0] = sources[0].model_copy(
            update={
                "requested_params": {
                    **sources[0].requested_params,
                    "start": "not a date",
                }
            }
        )
    directory = tmp_path / "must_not_publish"
    with pytest.raises(ValueError, match="provenance"):
        write_bundle(directory, req, result.model_copy(update={"provenance": sources}))
    assert not directory.exists()


def test_profile_helpers_are_api_safe_and_do_not_import_market_or_engine_sdks():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from packages.marketdata.native_research_fixture import fixture_profile_for_symbols, START, END
assert fixture_profile_for_symbols(['A','B','C','D']) == 'controlled_cross_section'
assert str(START) == '2024-01-02' and str(END) == '2024-03-01'
assert not {'pandas','pyarrow','exchange_calendars','akquant','yfinance','akshare','openbb'} & set(sys.modules)
""",
        ],
        check=True,
    )


def test_frozen_sample_change_fails_instead_of_reusing_the_profile_version(monkeypatch):
    from dataclasses import replace

    from packages.marketdata import native_research_fixture

    changed = replace(CROSS_SECTION_TAPE, sample_id="unreviewed replacement")
    monkeypatch.setattr(native_research_fixture, "CROSS_SECTION_TAPE", changed)
    with pytest.raises(ValueError, match="review and version"):
        PortableFixtureProvider().fetch_bars(request())


def test_profile_does_not_claim_live_or_expand_request_capacity():
    with pytest.raises(ValueError, match="live"):
        PortableFixtureProvider(mode="live")
    assert BarsRequest.model_fields["listings"].metadata[1].max_length == 100
    assert validate_fixture_request(request()) == PROFILE
    assert fixture_profile_for_symbols(["AAPL", "MSFT"]) is None
    capability = next(
        row
        for row in PortableFixtureProvider().capabilities()
        if row.endpoint == VERSION
    )
    assert capability.evidence == "fixture" and capability.status == "documented"
    assert capability.symbols == list(SYMBOLS)


def test_unrelated_real_symbol_a_is_not_relabelled_as_synthetic(tmp_path):
    listing = make_listing(
        symbol="A",
        mic="XNYS",
        currency="USD",
        timezone="America/New_York",
        calendar="XNYS",
        vendor="other",
    )
    req = request(["A"]).model_copy(update={"listings": [listing]})
    # No fixture metadata or identity: ordinary missing source data stays missing.
    manifest = write_bundle(
        tmp_path / "ordinary", req, finish_result(req, ProviderResult())
    )
    assert "fixture_profile" not in manifest
    assert not manifest["ready"]
