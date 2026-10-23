"""Offline contract/SDK-schema tests. No test in this file verifies live data."""

import json
import secrets
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pandas as pd
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from packages.marketdata.calendars import coverage_for
from packages.marketdata.catalog import provider_catalog
from packages.marketdata.contracts import (
    Bar,
    BarsRequest,
    Capability,
    PriceSemantics,
    ProviderResult,
    UniverseMember,
    UniverseSnapshot,
    UploadMapping,
    make_listing,
)
from packages.marketdata.normalize import finish_result, frame_payload, normalize_upload
from packages.marketdata.providers.akshare import AKShareProvider
from packages.marketdata.providers.common import (
    CallPolicy,
    ProviderError,
    budget_scope,
    classify_error,
    restore_frame,
    with_retries,
)
from packages.marketdata.providers.fixtures import akshare_bars, yfinance_bars
from packages.marketdata.providers.yfinance import YFinanceProvider
from packages.marketdata.storage import verify_bundle, write_bundle

FAST = CallPolicy(timeout=1, min_interval=0, backoff=0)


def listing(symbol="AAPL", mic="XNAS", vendor="yfinance"):
    return make_listing(
        symbol=symbol,
        mic=mic,
        vendor=vendor,
        currency="USD",
        timezone="America/New_York",
        calendar="XNAS",
    )


def request(listings=None, start="2024-01-02", end="2024-01-06", **kwargs):
    return BarsRequest(
        listings=listings or [listing()],
        start=date.fromisoformat(start),
        end=date.fromisoformat(end),
        as_of=datetime(2024, 7, 2, tzinfo=UTC),
        **kwargs,
    )


def bar(item=None, day="2024-01-02", **kwargs):
    return Bar(
        listing_id=(item or listing()).listing_id,
        session_date=date.fromisoformat(day),
        open=10,
        high=12,
        low=9,
        close=11,
        volume=100,
        currency="USD",
        volume_unit="shares",
        **kwargs,
    )


def test_identity_is_venue_scoped_stable_and_validated():
    assert listing().listing_id == listing().listing_id
    assert listing(mic="XNYS").listing_id != listing().listing_id
    with pytest.raises(ValidationError):
        listing().model_validate(listing().model_dump() | {"listing_id": "../secret"})
    with pytest.raises(ValidationError):
        request(end="2024-01-02")
    with pytest.raises(ValidationError):
        request([listing(), listing()])


def test_capability_cannot_masquerade_fixture_as_live():
    values = {
        "feature": "daily_bars",
        "connector": "x",
        "provider": "x",
        "endpoint": "x",
        "status": "verified",
        "evidence": "fixture",
        "symbols": ["AAPL"],
        "checked_at": datetime.now(UTC),
        "package_versions": {"x": "1"},
        "start": date(2024, 1, 2),
        "end": date(2024, 1, 3),
    }
    with pytest.raises(ValidationError):
        Capability(**values)
    assert Capability(**(values | {"evidence": "live"})).status == "verified"
    assert all(
        c["status"] != "verified" for p in provider_catalog() for c in p["capabilities"]
    )


@pytest.mark.parametrize(
    "field", ["open", "high", "low", "close", "volume", "adj_close", "amount"]
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_values_fail_closed(field, value):
    with pytest.raises(ValidationError):
        Bar.model_validate(bar().model_dump() | {field: value})


def test_coverage_missing_not_filled_and_weekend_is_not_missing():
    req = request()
    result = finish_result(req, ProviderResult(data=[bar(), bar(day="2024-01-05")]))
    assert len(result.data) == 2
    assert result.coverage[0].missing_sessions == [date(2024, 1, 3), date(2024, 1, 4)]
    assert result.coverage[0].status == "partial"
    assert (
        coverage_for(
            listing(), request(start="2024-01-06", end="2024-01-08"), []
        ).status
        == "complete"
    )
    assert (
        coverage_for(
            listing(), request(start="2024-07-04", end="2024-07-05"), []
        ).expected_sessions
        == []
    )


def test_real_china_holiday_calendar_and_bounds():
    cn = make_listing(
        symbol="600000",
        mic="XSHG",
        currency="CNY",
        timezone="Asia/Shanghai",
        calendar="XSHG",
        vendor="akshare",
    )
    cov = coverage_for(cn, request([cn], start="2024-02-09", end="2024-02-19"), [])
    assert cov.expected_sessions == []
    unknown = cn.model_copy(update={"calendar": "NO_SUCH_EXCHANGE"})
    assert coverage_for(unknown, request([unknown]), []).status == "unknown"
    assert (
        coverage_for(cn, request([cn], start="2099-01-02", end="2099-01-06"), []).status
        == "unknown"
    )


def test_calendar_dst_early_close_and_provisional():
    req = request(start="2024-11-29", end="2024-11-30").model_copy(
        update={"as_of": datetime(2024, 11, 29, 17, 59, tzinfo=UTC)}
    )
    result = finish_result(req, ProviderResult(data=[bar(day="2024-11-29")]))
    assert result.data[0].flags == ["provisional"]
    assert result.coverage[0].status == "partial"
    closed = req.model_copy(update={"as_of": datetime(2024, 11, 29, 18, 1, tzinfo=UTC)})
    assert (
        finish_result(closed, ProviderResult(data=[bar(day="2024-11-29")]))
        .coverage[0]
        .status
        == "complete"
    )


def test_listing_lifetime_is_not_invented_as_missing():
    item = listing().model_copy(
        update={"listed_on": date(2024, 1, 3), "delisted_on": date(2024, 1, 5)}
    )
    cov = coverage_for(
        item,
        request([item]),
        [bar(item, day="2024-01-03"), bar(item, day="2024-01-04")],
    )
    assert cov.status == "complete"
    assert cov.before_listing_sessions == [date(2024, 1, 2)]
    assert cov.after_delisting_sessions == [date(2024, 1, 5)]


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"high": 10}, "invalid_ohlc"),
        ({"volume": -1}, "negative_volume_or_amount"),
        ({"close": 0}, "nonpositive_price"),
    ],
)
def test_invalid_rows_block_readiness(changes, code):
    bad = bar().model_copy(update=changes)
    result = finish_result(request(), ProviderResult(data=[bad]))
    assert not result.data
    assert result.coverage[0].status == "failed"
    assert result.warnings[0].code == code


def test_duplicate_sessions_fail_instead_of_silent_last_write_wins():
    result = finish_result(
        request(),
        ProviderResult(data=[bar(), bar().model_copy(update={"close": 10.5})]),
    )
    assert len(result.data) == 1
    assert result.coverage[0].error_code == "duplicate_bar"


@pytest.mark.parametrize("mode", ["raw", "qfq", "hfq"])
def test_akshare_independent_modes_lots_and_boundaries(mode):
    provider = AKShareProvider(mode="fixture", policy=FAST)
    resolved = provider.resolve_instruments(["600000", "000001"])
    assert {x.mic for x in resolved.data} == {"XSHG", "XSHE"}
    result = provider.fetch_bars(request(resolved.data, price_mode=mode))
    assert len(result.data) == 8
    assert all(c.status == "complete" for c in result.coverage)
    assert result.data[0].volume == 100000
    assert result.price_semantics.native_mode == mode
    assert result.provenance[0].effective_params["end_date"] == "20240105"
    assert result.provenance[0].effective_params["adjust"] == (
        "" if mode == "raw" else mode
    )
    assert not result.actions
    assert all(p.evidence == "fixture" for p in result.provenance)


def test_yahoo_flags_adjclose_actions_and_split_semantics():
    seen = []

    class Client:
        def yfinance_bars(self, **kwargs):
            seen.append(kwargs)
            return yfinance_bars(kwargs["symbol"], kwargs["start"], kwargs["end"])

    provider = YFinanceProvider(mode="fixture", client=Client(), policy=FAST)
    result = provider.fetch_bars(request())
    assert len(result.data) == 4
    assert result.price_semantics.split_basis == "adjusted"
    assert result.price_semantics.dividend_basis == "unadjusted"
    assert result.price_semantics.adj_close_dividend_basis == "adjusted"
    assert {a.action_type for a in result.actions} == {"dividend", "split"}
    assert len({a.provenance_id for a in result.actions}) == 1
    assert result.actions[0].provenance_id != result.raw[0].provenance_id
    assert result.price_semantics.actions_snapshot_id
    assert (
        seen[0]
        | {"auto_adjust": False, "back_adjust": False, "repair": False, "actions": True}
        == seen[0]
    )
    with pytest.raises(ValueError):
        provider.fetch_bars(request(price_mode="raw"))


def test_upstream_single_listing_failure_and_schema_drift_are_partial_not_filled():
    marker = secrets.token_hex(32)

    class Client:
        def stock_zh_a_hist(self, **kwargs):
            if kwargs["symbol"] == "000001":
                raise TimeoutError(f"secret token={marker}")
            return akshare_bars("600000", "", kwargs["start_date"], kwargs["end_date"])

    resolver = AKShareProvider(mode="fixture", policy=FAST)
    req = request(resolver.resolve_instruments(["600000", "000001"]).data)
    result = AKShareProvider(mode="fixture", client=Client(), policy=FAST).fetch_bars(
        req
    )
    assert sorted(c.status for c in result.coverage) == ["complete", "failed"]
    assert marker not in result.model_dump_json()
    client = SimpleNamespace(
        stock_zh_a_hist=lambda **kwargs: pd.DataFrame({"renamed": [1]})
    )
    drift = AKShareProvider(mode="fixture", client=client, policy=FAST).fetch_bars(req)
    assert all(c.error_code == "upstream_schema_changed" for c in drift.coverage)
    assert len(drift.raw) == 2


def test_retry_budget_hook_every_attempt_and_no_retry_for_schema():
    calls, reservations = [], []

    def transient():
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("private")
        return 42

    with budget_scope(reservations.append):
        assert with_retries(transient, budget_key="yahoo:history", policy=FAST) == 42
    assert reservations == ["yahoo:history", "yahoo:history"]
    with pytest.raises(ProviderError, match="upstream_schema_changed"):
        with_retries(
            lambda: (_ for _ in ()).throw(ValueError("private")),
            budget_key="schema",
            policy=FAST,
        )
    assert "private" not in str(classify_error(ValueError("private")))


def test_raw_arrow_return_preserves_float_bits_index_and_nulls():
    frame = pd.DataFrame(
        {"price": [1.2345678901234567, None]},
        index=pd.date_range("2024-01-02", periods=2, tz="Asia/Shanghai"),
    )
    raw = frame_payload(frame, "source")
    restored = restore_frame(raw.content)
    pd.testing.assert_frame_equal(frame, restored)


def test_parquet_manifest_checksums_immutable_export_and_preview(tmp_path):
    provider = YFinanceProvider(mode="fixture", policy=FAST)
    req = request(provider.resolve_instruments(["AAPL"]).data)
    result = provider.fetch_bars(req)
    manifest = write_bundle(tmp_path, req, result)
    assert manifest["ready"]
    assert "data/INDEX.csv" in manifest["files"]
    assert manifest["universe"]["point_in_time"] is False
    assert manifest["evaluator_symbol_mapping"] == {req.listings[0].listing_id: "INDEX"}
    assert pq.read_table(tmp_path / "normalized.parquet").num_rows == 4
    assert len(json.loads((tmp_path / "preview.json").read_text())) == 4
    verify_bundle(tmp_path, manifest)
    with pytest.raises(FileExistsError):
        write_bundle(tmp_path, req, result)
    (tmp_path / "data/INDEX.csv").write_text("changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_bundle(tmp_path, manifest)


def test_unknown_upload_semantics_preserved_not_assumed(tmp_path):
    raw = tmp_path / "input.csv"
    raw.write_text("day,o,h,l,c,v\n2024-01-02,10,12,9,11,10\n")
    mapping = UploadMapping(
        symbol="AAPL",
        mic="XNAS",
        timezone="America/New_York",
        calendar="XNAS",
        columns={
            "session_date": "day",
            "open": "o",
            "high": "h",
            "low": "l",
            "close": "c",
            "volume": "v",
        },
    )
    result = normalize_upload(raw, mapping, request(end="2024-01-03"))
    assert result.price_semantics == PriceSemantics()
    assert result.data[0].currency == "unknown"
    assert result.data[0].volume_unit == "unknown"
    assert any(w.code == "unknown_uploaded_semantics" for w in result.warnings)


def test_pit_requires_knowledge_time_not_just_current_members():
    with pytest.raises(ValidationError):
        UniverseSnapshot(
            state="point_in_time_verified",
            as_of=datetime.now(UTC),
            members=[UniverseMember(listing_id=listing().listing_id)],
        )


def test_long_generated_fixture_has_real_exchange_sessions_and_no_live_evidence():
    for provider, symbols in [
        (AKShareProvider(mode="fixture", policy=FAST), ["600000", "000001"]),
        (YFinanceProvider(mode="fixture", policy=FAST), ["AAPL", "MSFT"]),
    ]:
        resolved = provider.resolve_instruments(symbols)
        result = provider.fetch_bars(request(resolved.data, end="2024-07-01"))
        assert all(
            len(c.observed_sessions) >= 84 and c.status == "complete"
            for c in result.coverage
        )
        assert all(p.evidence == "fixture" for p in result.provenance)
        assert any(w.code == "synthetic_fixture_not_live" for w in result.warnings)


def test_refresh_revision_cannot_overwrite_published_snapshot(tmp_path):
    provider = YFinanceProvider(mode="fixture", policy=FAST)
    req = request()
    first = provider.fetch_bars(req)
    original = write_bundle(tmp_path / "v1", req, first)
    revised_bar = first.data[0].model_copy(update={"close": first.data[0].close + 0.25})
    revised = first.model_copy(update={"data": [revised_bar, *first.data[1:]]})
    second = write_bundle(tmp_path / "v2", req, revised)
    assert original["bars_sha256"] != second["bars_sha256"]
    assert original["content_hash"] != second["content_hash"]
    verify_bundle(tmp_path / "v1", original)
    verify_bundle(tmp_path / "v2", second)


def test_parquet_expansion_guard_runs_before_materialization(tmp_path, monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = tmp_path / "oversized.parquet"
    path.write_bytes(b"PAR1")
    mapping = UploadMapping(
        symbol="AAPL",
        mic="XNAS",
        timezone="America/New_York",
        calendar="XNAS",
        columns={
            key: key
            for key in ["session_date", "open", "high", "low", "close", "volume"]
        },
    )

    class Parquet:
        metadata = SimpleNamespace(num_rows=1_000_001, num_row_groups=1)
        schema_arrow = pa.schema([("x", pa.float64())])

        def read(self):
            pytest.fail("Expanded data must not be read before metadata guard")

    monkeypatch.setattr(pq, "ParquetFile", lambda path: Parquet())
    with pytest.raises(ValueError, match="Expanded Parquet"):
        normalize_upload(path, mapping, request())


def test_invalid_iana_zone_is_validation_error():
    with pytest.raises(ValidationError, match="IANA timezone"):
        make_listing(
            symbol="AAPL",
            mic="XNAS",
            currency="USD",
            timezone="not/a/timezone",
            calendar="XNAS",
            vendor="yfinance",
        )


def test_same_us_listing_has_canonical_calendar_identity_across_providers():
    import exchange_calendars as xc

    from packages.marketdata.providers.openbb import OpenBBProvider

    yahoo = (
        YFinanceProvider(mode="fixture", policy=FAST)
        .resolve_instruments(["AAPL"])
        .data[0]
    )
    nasdaq = OpenBBProvider(mode="fixture").resolve_instruments(["AAPL"]).data[0]
    for field in [
        "instrument_id",
        "listing_id",
        "mic",
        "asset_type",
        "currency",
        "timezone",
        "calendar",
    ]:
        assert getattr(yahoo, field) == getattr(nasdaq, field)
    assert yahoo.mic == "XNAS" and yahoo.calendar == "XNYS"
    assert xc.get_calendar("XNAS") is xc.get_calendar("XNYS")


def test_provider_metadata_mismatch_blocks_normalized_identity():
    data = yfinance_bars("AAPL", "2024-01-02", "2024-01-06")
    data.attrs["history_metadata"] = {
        "symbol": "MSFT",
        "currency": "EUR",
        "exchangeTimezoneName": "Europe/Paris",
    }
    client = SimpleNamespace(yfinance_bars=lambda **kwargs: data)
    result = YFinanceProvider(mode="fixture", client=client, policy=FAST).fetch_bars(
        request()
    )
    assert result.coverage[0].error_code == "upstream_listing_metadata_changed"
    assert result.raw and not result.data


def test_akshare_wrong_response_symbol_is_not_attributed_to_requested_listing():
    provider = AKShareProvider(mode="fixture", policy=FAST)
    req = request(provider.resolve_instruments(["600000"]).data)
    data = akshare_bars("000001", "", "20240102", "20240105")
    client = SimpleNamespace(stock_zh_a_hist=lambda **kwargs: data)
    result = AKShareProvider(mode="fixture", client=client, policy=FAST).fetch_bars(req)
    assert result.coverage[0].error_code == "upstream_symbol_mismatch"
    assert result.raw and not result.data


def upload_mapping(**changes):
    values = {
        "symbol": "AAPL",
        "mic": "XNAS",
        "timezone": "America/New_York",
        "calendar": "XNYS",
        "columns": {
            key: key
            for key in ["session_date", "open", "high", "low", "close", "volume"]
        },
    }
    return UploadMapping(**(values | changes))


def test_upload_preserves_exact_original_once_not_expanded_dataframe(
    tmp_path, monkeypatch
):
    import base64
    import hashlib

    import packages.marketdata.normalize as normalization

    original = b"session_date,open,high,low,close,volume,unused\n2024-01-02,10,11,9,10,100,secret-extra-column\n"
    path = tmp_path / "source.csv"
    path.write_bytes(original)
    monkeypatch.setattr(
        normalization,
        "frame_payload",
        lambda *a, **k: pytest.fail("Upload must not expand raw frame"),
    )
    inline = normalize_upload(path, upload_mapping(), request(end="2024-01-03"))
    assert base64.b64decode(inline.raw[0].content["bytes_base64"]) == original
    reference = {
        "artifact": "source.csv",
        "sha256": hashlib.sha256(original).hexdigest(),
        "size_bytes": len(original),
    }
    result = normalize_upload(
        path, upload_mapping(), request(end="2024-01-03"), source_reference=reference
    )
    assert result.raw[0].content == reference
    assert len(result.data) == 1
    for bad in [
        reference | {"artifact": "../source.csv"},
        reference | {"sha256": "0" * 64},
        reference | {"size_bytes": 1},
    ]:
        with pytest.raises(ValueError, match="source reference"):
            normalize_upload(path, upload_mapping(), request(), source_reference=bad)


@pytest.mark.parametrize("file_format", ["csv", "parquet"])
def test_native_upload_stages_only_windowed_evaluator_inputs(tmp_path, file_format):
    """Real upload normalization/bundle, inert staging; no SDK or strategy runs."""
    import csv
    import hashlib
    import io

    from packages.marketdata.normalize import canonical_json
    from packages.tradegrad_adapter.config import (
        MarketConfig,
        RunConfig,
        canonical_bytes,
    )
    from packages.tradegrad_adapter.windows import stage_dataset_window

    days = [f"2024-01-{day:02d}" for day in (2, 3, 4, 5, 8, 9, 10, 11, 12)]
    original = (
        "session_date,open,high,low,close,volume,unused\n"
        + "".join(f"{day},10,11,9,10,100,not-evaluator-input\n" for day in days)
    ).encode()
    if file_format == "parquet":
        original = pd.read_csv(io.BytesIO(original)).to_parquet(index=False)
    source_name = f"source.{file_format}"
    path = tmp_path / source_name
    path.write_bytes(original)
    reference = {
        "artifact": source_name,
        "sha256": hashlib.sha256(original).hexdigest(),
        "size_bytes": len(original),
    }
    req = request(end="2024-01-13")
    result = normalize_upload(
        path,
        upload_mapping(currency="USD", volume_unit="shares"),
        req,
        source_reference=reference,
    )
    assert result.raw[0].content == reference
    assert result.raw[0].provenance_id == result.provenance[0].provenance_id
    assert result.provenance[0].evidence == "uploaded"
    root = tmp_path / "bundle"
    manifest = write_bundle(root, req, result)
    assert manifest["ready"]
    # Mirror the worker's original-byte and separately pinned preview publication.
    (root / source_name).write_bytes(original)
    manifest["artifact_files"][source_name] = reference["sha256"]
    manifest["upload_source"] = reference
    listing_id = req.listings[0].listing_id
    preview_name = f"preview/{listing_id}/0.json"
    preview = (root / "preview.json").read_bytes()
    preview_path = root / preview_name
    preview_path.parent.mkdir(parents=True)
    preview_path.write_bytes(preview)
    manifest["preview_files"] = {
        listing_id: [
            {
                "path": preview_name,
                "sha256": hashlib.sha256(preview).hexdigest(),
                "start_date": str(req.start),
                "end_date": str(req.end),
            }
        ]
    }
    manifest.pop("content_hash")
    manifest["content_hash"] = hashlib.sha256(canonical_json(manifest)).hexdigest()
    encoded = canonical_bytes(manifest)
    (root / "manifest.json").write_bytes(encoded)
    verify_bundle(root, manifest)
    ref = {"id": "test-version", "sha256": "a" * 64}
    config = RunConfig(
        execution_mode="optimization",
        task="us_cross",
        dataset={"id": "test-upload", "sha256": hashlib.sha256(encoded).hexdigest()},
        seed_strategy=ref,
        universe=ref,
        windows={
            "train": {"start": "2024-01-08", "end": "2024-01-10"},
            "validation": {"start": "2024-01-10", "end": "2024-01-12"},
            "holdout": {"start": "2024-01-12", "end": "2024-01-13"},
        },
        warmup={"sessions": 3},
        market_profile=MarketConfig.for_market("US"),
        environment_digest="b" * 64,
    )
    reads = []

    def read_file(name):
        reads.append(name)
        return (root / name).read_bytes()

    staged = stage_dataset_window(config, "train", manifest, read_file)
    assert set(reads) == set(manifest["files"])
    assert not set(reads) & (set(manifest["artifact_files"]) | {preview_name})
    assert set(staged.files) == {
        "data/INDEX.csv",
        "universe/members_list_2024-01-02.csv",
    }
    rows = list(csv.DictReader(io.StringIO(staged.files["data/INDEX.csv"].decode())))
    assert [row["date"] for row in rows] == days[1:6]
    assert "unused" not in rows[0]
    assert (root / source_name).read_bytes() == original
    assert (root / "manifest.json").read_bytes() == canonical_bytes(manifest) == encoded
    verify_bundle(root, manifest)


@pytest.mark.parametrize(
    "line", ["2023-12-31,2024-01-02,10,11,9,10,100", "2024-01-02,10,11,9,10"]
)
def test_upload_rejects_shifted_or_short_csv_records(tmp_path, line):
    path = tmp_path / "source.csv"
    path.write_text("session_date,open,high,low,close,volume\n" + line + "\n")
    with pytest.raises(ValueError, match="record width"):
        normalize_upload(path, upload_mapping(), request())


def test_upload_rejects_foreign_calendar_and_canonicalizes_true_alias():
    with pytest.raises(ValidationError, match="Calendar does not match"):
        upload_mapping(calendar="XSHG")
    assert upload_mapping(calendar="XNAS").calendar == "XNYS"
    from packages.marketdata.venues import validate_listing_calendar

    assert validate_listing_calendar("XSHE", "XSHG") == "XSHG"
    with pytest.raises(ValueError):
        validate_listing_calendar("XSHG", "XNYS")


def test_upload_us_holiday_never_becomes_valid_trading_day(tmp_path):
    path = tmp_path / "source.csv"
    path.write_text(
        "session_date,open,high,low,close,volume\n2024-01-15,10,11,9,10,100\n"
    )
    result = normalize_upload(
        path, upload_mapping(), request(start="2024-01-15", end="2024-01-16")
    )
    assert result.coverage[0].status != "complete"
    assert result.coverage[0].non_session_dates == [date(2024, 1, 15)]


@pytest.mark.parametrize(
    "missing", [["Dividends"], ["Stock Splits"], ["Dividends", "Stock Splits"]]
)
def test_yahoo_missing_event_fields_never_implies_authoritative_empty_events(missing):
    data = yfinance_bars("AAPL", "2024-01-02", "2024-01-06").drop(columns=missing)
    client = SimpleNamespace(yfinance_bars=lambda **kwargs: data)
    result = YFinanceProvider(mode="fixture", client=client, policy=FAST).fetch_bars(
        request()
    )
    assert result.coverage[0].status == "failed"
    assert result.coverage[0].error_code == "upstream_actions_schema_changed"
    assert result.raw
    assert list(restore_frame(result.raw[0].content).columns) == list(data.columns)
    assert not result.actions and result.price_semantics.actions_snapshot_id is None


@pytest.mark.parametrize(
    "provider_name,price_mode",
    [
        ("akshare", "raw"),
        ("akshare", "qfq"),
        ("akshare", "hfq"),
        ("yfinance", "provider_native"),
    ],
)
def test_old_coverage_cutoff_never_becomes_adjustment_state_evidence(
    provider_name, price_mode
):
    from packages.marketdata.providers.registry import get_provider

    provider = get_provider(provider_name, mode="fixture")
    symbols = ["600000"] if provider_name == "akshare" else ["AAPL"]
    listings = provider.resolve_instruments(symbols).data
    old = datetime(2010, 1, 1, tzinfo=UTC)
    req = request(listings, price_mode=price_mode).model_copy(update={"as_of": old})
    result = provider.fetch_bars(req)
    assert result.price_semantics.adjustment_as_of is None
    assert "coverage cutoff" in result.price_semantics.note
    assert all(p.fetched_at > old for p in result.provenance)
    assert (
        result.coverage[0].provisional_sessions == result.coverage[0].observed_sessions
    )
    assert len(result.coverage[0].provisional_sessions) == 4
    assert result.coverage[0].status == "partial"
    finished = finish_result(req, result)
    assert finished.price_semantics.adjustment_as_of is None
    assert finished.coverage == result.coverage


def test_normalization_never_replaces_explicit_adjustment_timestamp_with_acquisition():
    from packages.marketdata.normalize import provenance

    explicit = datetime(2020, 1, 1, tzinfo=UTC)
    result = ProviderResult(
        data=[bar()],
        price_semantics=PriceSemantics(adjustment_as_of=explicit),
        provenance=[provenance("upload", "user", "test", evidence="uploaded")],
    )
    normalized = finish_result(request(), result)
    assert normalized.price_semantics.adjustment_as_of == explicit


@pytest.mark.parametrize("mic", ["ARCX", "BATS", "IEXG"])
def test_declared_additional_us_venues_share_explicit_proxy_without_foreign_calendars(
    mic,
):
    from packages.marketdata.venues import canonical_calendar

    assert (
        canonical_calendar(mic, "XNYS", currency="USD", timezone="America/New_York")
        == "XNYS"
    )
    assert (
        canonical_calendar(mic, "XNAS", currency="USD", timezone="America/New_York")
        == "XNYS"
    )
    assert upload_mapping(mic=mic).calendar == "XNYS"
    with pytest.raises(ValueError, match="Calendar does not match"):
        canonical_calendar(mic, "XSHG", currency="USD", timezone="America/New_York")
