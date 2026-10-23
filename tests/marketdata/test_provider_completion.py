"""Pure contracts/fixtures only; no SDK subprocess, network, DB or strategy runs."""

import hashlib
import json
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.data_schemas import ProviderProbeCreate, ProviderTestReceipt
from packages.marketdata.contracts import BarsRequest, RepairSource
from packages.marketdata.normalize import UPSTREAM_TERMS
from packages.marketdata.probes import build_probe_receipt
from packages.marketdata.providers.common import CallPolicy
from packages.marketdata.providers.registry import get_provider
from packages.marketdata.providers.yfinance import repair_flags
from packages.marketdata.storage import verify_bundle, write_bundle

FAST = CallPolicy(timeout=1, attempts=1, min_interval=0, backoff=0)


def provider(name):
    value = get_provider(name, mode="fixture")
    value.policy = FAST
    return value


def request(value, symbols, *, start="2024-01-02", end="2024-01-06", **kwargs):
    return BarsRequest(
        listings=value.resolve_instruments(symbols).data,
        start=start,
        end=end,
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        **kwargs,
    )


def parent_reference(manifest):
    return RepairSource(
        dataset_version_id=uuid4(),
        manifest_sha256="1" * 64,
        content_hash=manifest["content_hash"],
        raw_sha256=manifest["artifact_files"]["raw.json"],
        provenance_sha256=manifest["artifact_files"]["provenance.json"],
    )


@pytest.mark.parametrize(
    "name,symbol,upstream",
    [
        ("akshare", "600000", "eastmoney"),
        ("yfinance", "AAPL", "yahoo"),
        ("openbb", "MSFT", "nasdaq"),
    ],
)
def test_terms_are_new_acquisition_metadata_not_sdk_licence(
    name, symbol, upstream, tmp_path
):
    value = provider(name)
    query = request(value, [symbol])
    result = value.fetch_bars(query)
    assert all(p.terms_url == UPSTREAM_TERMS[upstream] for p in result.provenance)
    bundle = write_bundle(tmp_path / name, query, result)
    assert all(p["terms_url"] == UPSTREAM_TERMS[upstream] for p in bundle["provenance"])
    assert all(
        p["provider"] == upstream and p["fetched_at"] for p in bundle["provenance"]
    )
    # Simulate an old already-published manifest; reading/verifying doesn't insert
    # optional fields or update its hashes. The production reads raw JSON too.
    old = json.loads(json.dumps(bundle))
    for entry in old["provenance"]:
        entry.pop("terms_url")
    old.pop("content_hash")
    from packages.marketdata.normalize import canonical_json

    old["content_hash"] = hashlib.sha256(canonical_json(old)).hexdigest()
    encoded = canonical_json(old)
    verify_bundle(tmp_path / name, old)
    assert canonical_json(old) == encoded


def test_explicit_repair_preserves_original_and_pins_parent(tmp_path):
    value = provider("yfinance")
    query = request(value, ["AAPL"])
    original_result = value.fetch_bars(query)
    original = write_bundle(tmp_path / "original", query, original_result)
    before = {
        p.name: p.read_bytes() for p in (tmp_path / "original").iterdir() if p.is_file()
    }
    reference = parent_reference(original)
    repaired_query = BarsRequest(
        **{**query.model_dump(), "price_mode": "repair", "repair_source": reference}
    )
    repaired_result = value.fetch_bars(repaired_query)
    repaired = write_bundle(tmp_path / "derived", repaired_query, repaired_result)
    assert repaired["derivation"] == reference.model_dump(mode="json")
    assert repaired["content_hash"] != original["content_hash"]
    assert repaired["price_semantics"]["split_basis"] == "adjusted"
    assert repaired["price_semantics"]["dividend_basis"] == "unadjusted"
    assert (
        repaired["price_semantics"]["algorithm_version"] == "yahoo-native;repair=True"
    )
    assert all(p.effective_params["repair"] is True for p in repaired_result.provenance)
    assert all(
        p.effective_params["repair"] is False for p in original_result.provenance
    )
    assert all(
        b.flags == ["yahoo_repair_requested", "yahoo_repair_not_flagged"]
        for b in repaired_result.data
    )
    assert all(not b.flags for b in original_result.data)
    assert all(
        p.read_bytes() == before[p.name]
        for p in (tmp_path / "original").iterdir()
        if p.is_file()
    )
    verify_bundle(tmp_path / "original", original)
    verify_bundle(tmp_path / "derived", repaired)
    for kwargs in ({"price_mode": "repair"}, {"repair_source": reference}):
        with pytest.raises(ValidationError, match="Repair requires"):
            request(value, ["AAPL"], **kwargs)


@pytest.mark.parametrize(
    "value,flag",
    [
        (True, "yahoo_repaired"),
        (False, "yahoo_repair_not_flagged"),
        (None, "yahoo_repair_status_unknown"),
        ("False", "yahoo_repair_status_unknown"),
    ],
)
def test_repair_flags_are_not_invented(value, flag):
    assert repair_flags(value) == ["yahoo_repair_requested", flag]


@pytest.mark.parametrize(
    "name,symbols",
    [("akshare", ["600000", "000001"]), ("yfinance", ["AAPL"]), ("openbb", ["MSFT"])],
)
def test_distinct_research_fixture_window_supports_warmup_without_altering_old_rows(
    name, symbols
):
    value = provider(name)
    old = value.fetch_bars(request(value, symbols))
    assert len(old.data) == 4 * len(symbols)
    query = request(value, symbols, start="2023-07-03", end="2024-01-02")
    result = value.fetch_bars(query)
    assert all(c.status == "complete" for c in result.coverage)
    assert all(p.evidence == "fixture" for p in result.provenance)
    assert all(
        len(
            [
                b
                for b in result.data
                if b.listing_id == item.listing_id and b.session_date < date(2023, 9, 1)
            ]
        )
        >= 20
        for item in query.listings
    )
    subset = value.fetch_bars(
        BarsRequest(
            **{**query.model_dump(), "start": "2023-09-01", "end": "2023-10-01"}
        )
    )
    assert [
        b for b in result.data if date(2023, 9, 1) <= b.session_date < date(2023, 10, 1)
    ] == subset.data
    assert value.fetch_bars(request(value, symbols)).data == old.data
    # Exact original default half-year size remains unchanged.
    full = value.fetch_bars(request(value, symbols, end="2024-07-01"))
    assert len(full.data) == (117 * len(symbols) if name == "akshare" else 124)


def receipt(evidence="fixture", complete=True, versions=None, warnings=()):
    return build_probe_receipt(
        import_id=uuid4(),
        provider="yfinance",
        request={
            "symbols": ["AAPL"],
            "start_date": "2024-01-02",
            "end_date": "2024-01-06",
        },
        provenance=[
            {
                "evidence": evidence,
                "endpoint": "Ticker.history",
                "package_versions": versions or {"yfinance": "1.7.0"},
            }
        ],
        warnings=warnings,
        complete=complete,
    )


def test_probe_receipt_distinguishes_fixture_live_partial_and_failures():
    fixture = receipt()
    assert (
        fixture["outcome"] == "fixture_passed"
        and fixture["capability_status"] == "documented"
    )
    live = receipt("live")
    assert live["outcome"] == "verified" and live["capability_status"] == "verified"
    assert ProviderTestReceipt.model_validate(live).symbol == "AAPL"
    assert receipt("live", False)["capability_status"] == "unknown"
    assert (
        receipt("live", versions={"yfinance": "not-installed"})["capability_status"]
        == "unknown"
    )
    assert (
        receipt(warnings=[{"code": "rate_limited", "severity": "error"}])["outcome"]
        == "failed"
    )


@pytest.mark.parametrize(
    "start,end",
    [
        ("2024-01-02", "2024-01-17"),
        ("2024-01-02", "2024-01-02"),
        ("2099-01-02", "2099-01-06"),
    ],
)
def test_probe_is_bounded_to_closed_fortnight(start, end):
    with pytest.raises(ValidationError):
        ProviderProbeCreate(symbol="AAPL", start_date=start, end_date=end)


def test_probe_failure_codes_do_not_echo_arbitrary_upstream_content():
    result = receipt(
        warnings=[
            {"code": "https://example.invalid/?token=private", "severity": "error"}
        ]
    )
    assert result["error_codes"] == ["provider_test_failed"]
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize(
    "name,symbols,task",
    [
        ("akshare", ["600000", "000001"], "cn_cross"),
        ("yfinance", ["AAPL"], "us_time"),
        ("openbb", ["MSFT"], "us_time"),
    ],
)
def test_exact_provider_fixture_snapshot_passes_corrected_window_staging_without_strategy_execution(
    name, symbols, task, tmp_path
):
    import csv
    import io

    from packages.tradegrad_adapter.config import (
        MarketConfig,
        RunConfig,
        canonical_bytes,
    )
    from packages.tradegrad_adapter.windows import PreflightError, stage_dataset_window

    value = provider(name)
    query = request(value, symbols, start="2023-07-03", end="2024-01-02")
    bundle = write_bundle(tmp_path / name, query, value.fetch_bars(query))
    reference = {"id": str(uuid4()), "sha256": "a" * 64}
    config = RunConfig.model_validate(
        {
            "task": task,
            "execution_mode": "optimization",
            "dataset": {
                "id": str(uuid4()),
                "sha256": hashlib.sha256(canonical_bytes(bundle)).hexdigest(),
            },
            "seed_strategy": reference,
            "universe": reference,
            "windows": {
                "train": {"start": "2023-09-01", "end": "2023-10-16"},
                "validation": {"start": "2023-10-16", "end": "2023-12-01"},
                "holdout": {"start": "2023-12-01", "end": "2023-12-29"},
            },
            "warmup": {"sessions": 20},
            "market_profile": MarketConfig.for_market(task[:2].upper()).model_dump(
                mode="json"
            ),
            "optimizer": {"max_iterations": 1},
            "environment_digest": "b" * 64,
        }
    )
    read = lambda path: (tmp_path / name / path).read_bytes()
    for split in ("train", "validation"):
        staged = stage_dataset_window(config, split, bundle, read)
        assert all(r.staged_warmup_sessions == 20 for r in staged.report.listings)
        assert all(
            r.source_sha256
            == bundle["files"][
                f"data/{bundle['evaluator_symbol_mapping'][r.listing_id]}.csv"
            ]
            for r in staged.report.listings
        )
        for path, content in staged.files.items():
            if path.startswith("data/"):
                dates = [
                    row["date"] for row in csv.DictReader(io.StringIO(content.decode()))
                ]
                assert (
                    sum(day < str(config.windows.get(split).start) for day in dates)
                    == 20
                )
                assert max(dates) < "2023-12-01"
    with pytest.raises(PreflightError, match="holdout"):
        stage_dataset_window(config, "holdout", bundle, read)


def test_yahoo_adapter_keeps_sdk_repair_evidence_and_never_invents_adjustment_basis(
    tmp_path,
):
    from types import SimpleNamespace

    from packages.marketdata.providers.fixtures import yfinance_bars

    value = provider("yfinance")
    query = request(value, ["AAPL"])
    original = write_bundle(tmp_path / "parent", query, value.fetch_bars(query))
    calls = []

    def repaired(**kwargs):
        calls.append(kwargs)
        frame = yfinance_bars(kwargs["symbol"], kwargs["start"], kwargs["end"])
        frame["Repaired?"] = [True, False, False, False]
        return frame

    value.client = SimpleNamespace(yfinance_bars=repaired)
    result = value.fetch_bars(
        BarsRequest(
            **{
                **query.model_dump(),
                "price_mode": "repair",
                "repair_source": parent_reference(original),
            }
        )
    )
    assert calls[0]["repair"] is True
    assert calls[0]["auto_adjust"] is False and calls[0]["back_adjust"] is False
    assert result.data[0].flags == ["yahoo_repair_requested", "yahoo_repaired"]
    assert result.price_semantics.split_basis == "adjusted"
    assert result.raw[0].content["table"]["columns"][-1] == "Repaired?"
