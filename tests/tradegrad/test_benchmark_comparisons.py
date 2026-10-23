"""Pure immutable-input/math tests: never execute a candidate or engine."""

import copy
import hashlib
import math
from uuid import UUID

import pytest

from backend.app.benchmark_services import comparison_view
from backend.tests.research_view_fixtures import synthetic_result
from packages.tradegrad_adapter.benchmarks import (
    account_observations,
    attach_comparison,
    build_comparison,
    prepare_benchmark,
    require_comparison_binding,
    validate_comparison,
)
from packages.tradegrad_adapter.config import BenchmarkConfig, canonical_bytes
from packages.tradegrad_adapter.results import validate_result_bundle


def fixture(days=None):
    days = days or ["2024-02-01", "2024-02-02", "2024-02-05"]
    listing = {
        "listing_id": str(UUID(int=1)),
        "symbol": "BENCH",
        "currency": "CNY",
        "timezone": "Asia/Shanghai",
        "calendar": "XSHG",
    }
    raw = (
        "date,open,high,low,close,volume\n"
        + "\n".join(
            f"{day},{100 + i},{101 + i},{99 + i},{100 + i},1000"
            for i, day in enumerate(days)
        )
        + "\n"
    ).encode()
    manifest = {
        "request": {"interval": "1d", "as_of": "2026-10-23T00:00:00Z"},
        "listings": [listing],
        "price_semantics": {
            "native_mode": "raw",
            "split_basis": "unadjusted",
            "dividend_basis": "unadjusted",
        },
        "coverage": [
            {
                "listing_id": listing["listing_id"],
                "calendar": "XSHG",
                "calendar_version": "test-pinned.v1",
                "expected_sessions": days,
            }
        ],
        "evaluator_symbol_mapping": {listing["listing_id"]: "BENCH"},
        "files": {"data/BENCH.csv": hashlib.sha256(raw).hexdigest()},
    }
    reference = BenchmarkConfig(
        dataset={
            "id": str(UUID(int=2)),
            "sha256": hashlib.sha256(canonical_bytes(manifest)).hexdigest(),
        },
        listing_id=listing["listing_id"],
    )
    return reference, manifest, raw


def prepared(days=None):
    reference, manifest, raw = fixture(days)
    return prepare_benchmark(
        reference,
        manifest,
        manifest,
        lambda _: raw,
        {"start": "2024-02-01", "end": "2024-02-10"},
        "CN",
    )


def test_exact_math_observed_anchor_units_and_no_invented_cash():
    evidence = prepared()
    account = [
        [f"{day}T15:00:00+08:00", equity]
        for day, equity in zip(
            [r[0] for r in evidence["observations"]], [95, 100, 104], strict=True
        )
    ]
    value = build_comparison(evidence, account)
    assert value["normalization"] == {
        "session": "2024-02-01",
        "strategy_equity": 95,
        "benchmark_close": 100,
    }
    assert value["series"]["rows"][0][4:] == [0, 0, 0]
    assert value["metrics"][0]["value"] == pytest.approx((104 / 95 - 1) * 100)
    assert value["metrics"][1]["value"] == pytest.approx(2)
    assert value["metrics"][2]["value"] == pytest.approx((104 / 95 - 1) * 100 - 2)
    assert value["metrics"][2]["unit"] == "percentage_points"
    assert (
        "not total return" in value["reason"] and "not cost-matched" in value["reason"]
    )


@pytest.mark.parametrize("scale", [0.001, 0.5, 1, 3, 1_000_000])
@pytest.mark.parametrize("growth", [-0.75, -0.01, 0, 0.1, 5])
def test_return_scale_invariance_and_excess_identity(scale, growth):
    evidence = prepared()
    days = [row[0] for row in evidence["observations"]]
    baseline = [
        [f"{day}T15:00:00+08:00", 100 * (1 + growth * i / 2)]
        for i, day in enumerate(days)
    ]
    result = build_comparison(evidence, baseline)
    scaled = copy.deepcopy(evidence)
    for row in scaled["observations"]:
        row[1] *= scale
    other = build_comparison(scaled, [[t, value * scale] for t, value in baseline])
    for original, actual in zip(
        result["series"]["rows"], other["series"]["rows"], strict=True
    ):
        assert actual[4:] == pytest.approx(original[4:])
        assert actual[6] == pytest.approx(actual[4] - actual[5])
    assert math.isclose(result["metrics"][0]["value"], growth * 100, abs_tol=1e-10)


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "timezone", "zero_anchor", "nan", "future"]
)
def test_account_alignment_fails_without_forward_fill(mutation):
    evidence = prepared()
    rows = [[f"{day}T15:00:00+08:00", 100] for day, _ in evidence["observations"]]
    if mutation == "missing":
        rows.pop(1)
    if mutation == "duplicate":
        rows.insert(1, rows[0])
    if mutation == "timezone":
        rows[0][0] = "2024-02-01T15:00:00"
    if mutation == "zero_anchor":
        rows[0][1] = 0
    if mutation == "nan":
        rows[1][1] = float("nan")
    if mutation == "future":
        rows[-1][0] = "2024-02-10T15:00:00+08:00"
    with pytest.raises(ValueError):
        build_comparison(evidence, rows)


@pytest.mark.parametrize(
    "mutation",
    [
        "hash",
        "unknown",
        "adjusted",
        "dividends",
        "currency",
        "timezone",
        "calendar",
        "calendar_version",
        "missing_session",
        "extra_session",
        "asof",
        "one_session",
        "provisional",
        "source_changed",
    ],
)
def test_exact_preflight_rejects_incompatible_evidence(mutation):
    ref, manifest, raw = fixture()
    strategy = copy.deepcopy(manifest)
    if mutation == "unknown":
        manifest["price_semantics"]["split_basis"] = "unknown"
    if mutation == "adjusted":
        manifest["price_semantics"]["split_basis"] = "adjusted"
    if mutation == "dividends":
        manifest["price_semantics"]["dividend_basis"] = "adjusted"
    if mutation in {"currency", "timezone", "calendar"}:
        manifest["listings"][0][mutation] = "wrong"
    if mutation == "calendar_version":
        manifest["coverage"][0]["calendar_version"] = "other"
    if mutation == "missing_session":
        raw = raw.replace(b"2024-02-02,101,102,100,101,1000\n", b"")
    if mutation == "extra_session":
        raw += b"2024-02-06,103,104,102,103,1000\n"
    if mutation == "asof":
        manifest["request"]["as_of"] = "2024-02-01"
    if mutation == "one_session":
        manifest["coverage"][0]["expected_sessions"] = ["2024-02-01"]
    if mutation == "provisional":
        manifest["coverage"][0]["provisional_sessions"] = ["2024-02-01"]
    if mutation == "source_changed":
        strategy["price_semantics"]["split_basis"] = "unknown"
    if mutation != "hash":
        manifest["files"]["data/BENCH.csv"] = hashlib.sha256(raw).hexdigest()
        ref = ref.model_copy(
            update={
                "dataset": ref.dataset.model_copy(
                    update={
                        "sha256": hashlib.sha256(canonical_bytes(manifest)).hexdigest()
                    }
                )
            }
        )
    else:
        raw += b"\n"
    with pytest.raises(ValueError):
        prepare_benchmark(
            ref,
            manifest,
            strategy,
            lambda _: raw,
            {"start": "2024-02-01", "end": "2024-02-10"},
            "CN",
        )


def test_future_prices_not_in_prepared_window_and_immutable_file_tamper_fails():
    ref, manifest, raw = fixture(
        ["2024-02-01", "2024-02-02", "2024-02-05", "2024-02-12"]
    )
    result = prepare_benchmark(
        ref,
        manifest,
        manifest,
        lambda _: raw,
        {"start": "2024-02-01", "end": "2024-02-10"},
        "CN",
    )
    assert [r[0] for r in result["observations"]] == [
        "2024-02-01",
        "2024-02-02",
        "2024-02-05",
    ]
    assert "2024-02-12" not in str(result)
    with pytest.raises(ValueError, match="Immutable input"):
        prepare_benchmark(
            ref,
            manifest,
            manifest,
            lambda _: raw.replace(b",103,", b",999,"),
            {"start": "2024-02-01", "end": "2024-02-10"},
            "CN",
        )


def test_native_durable_enrichment_validates_and_none_bytes_stay_exact():
    result = synthetic_result()
    original = copy.deepcopy(result.bundle)
    assert attach_comparison(original, None) is original
    days = [timestamp[:10] for timestamp, _ in account_observations(original)]
    evidence = prepared(days)
    bundle = attach_comparison(original, evidence)
    validate_result_bundle(bundle)
    assert (
        original == result.bundle
        and original["benchmark"]["status"] == "not_configured"
    )
    assert bundle["capabilities"]["benchmark"] is True
    changed = copy.deepcopy(bundle)
    changed["benchmark"]["metrics"][0]["value"] += 1
    with pytest.raises(ValueError, match="reconstruct"):
        validate_comparison(changed["benchmark"], changed)
    with pytest.raises(ValueError, match="Unrequested"):
        require_comparison_binding(
            bundle,
            __import__(
                "packages.tradegrad_adapter.config", fromlist=["RunConfig"]
            ).RunConfig.model_validate(result.run.config),
        )
    view = comparison_view(
        bundle["benchmark"], start="2024-02-03", end="2024-02-08", max_points=3
    )
    assert len(view["series"]["rows"]) == 3 and view["downsampled"]
    assert view["metrics"] == bundle["benchmark"]["metrics"]
    assert view["normalization"] == bundle["benchmark"]["normalization"]
    assert "observations" not in view["input"]


@pytest.mark.parametrize("invalid", [True, float("inf"), -(10**1000), 10**1000])
def test_nonfinite_or_unbounded_numbers_are_rejected_as_validation_errors(invalid):
    data = prepared()
    account = [[f"{day}T15:00:00+08:00", 100] for day, _ in data["observations"]]
    account[0][1] = invalid
    with pytest.raises(ValueError):
        build_comparison(data, account)
