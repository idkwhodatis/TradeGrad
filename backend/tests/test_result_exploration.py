"""Pure contracts and real checksum-bound files only; no engine/DB/socket starts."""

import copy
import hashlib
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.app.research_views import query_result_table
from backend.app.result_exploration import price_replay, return_analysis
from backend.app.services import DomainError
from backend.tests.research_view_fixtures import synthetic_result
from packages.tradegrad_adapter.indicators import (
    export_recorded_indicators,
    validate_indicators,
)
from packages.tradegrad_adapter.results import (
    bundle_sections,
    canonical_json_bytes,
    validate_result_bundle,
)


def recorded_payload():
    timestamp = (
        int(
            (
                datetime(2024, 2, 1, 2, tzinfo=UTC) - datetime(1970, 1, 1, tzinfo=UTC)
            ).total_seconds()
        )
        * 1_000_000_000
    )
    return {
        "definitions": [
            {
                "indicator_key": "declared_signal",
                "display_name": "Explicit observed signal",
                "pane": 0,
                "render_type": "line",
                "unit": "currency_per_share",
                "precision": 4,
                "color": "",
                "reference_lines": [],
                "scale_group": "",
            }
        ],
        "instances": [
            {
                "instance_id": "ins_1",
                "owner_strategy_id": "_default",
                "symbol": "target_symbol",
                "indicator_key": "declared_signal",
                "meta_json": "{}",
            }
        ],
        "points": [
            {
                "instance_id": "ins_1",
                "owner_strategy_id": "_default",
                "symbol": "target_symbol",
                "indicator_key": "declared_signal",
                "timestamp": timestamp,
                "timestamp_ms": timestamp // 1_000_000,
                "value": 9.123456789123,
                "warmup": False,
            }
        ],
    }


def with_section(bundle, payload=None):
    bundle["indicators"] = export_recorded_indicators(payload or recorded_payload())
    bundle["artifacts"] = [
        {
            "section": name,
            "media_type": "application/json",
            "sha256": hashlib.sha256(canonical_json_bytes(bundle[name])).hexdigest(),
            "bytes": len(canonical_json_bytes(bundle[name])),
        }
        for name in bundle_sections(bundle)
    ]
    return bundle


def stage_prices(result, root, days=("2024-02-01", "2024-02-02")):
    result.dataset.storage_key = result.dataset.storage_key or "synthetic-snapshot"
    listing = next(iter(result.dataset.manifest["evaluator_symbol_mapping"]))
    bars = [
        {
            "listing_id": listing,
            "session_date": day,
            "open": 100.123456789,
            "high": 102.0,
            "low": 99.0,
            "close": 101.0,
            "volume": 1234.5,
            "currency": "CNY",
            "volume_unit": "shares",
            "flags": ["synthetic_test"],
        }
        for day in days
    ]
    raw = canonical_json_bytes(bars)
    path = root / result.dataset.storage_key / "preview.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    result.dataset.manifest["preview_files"] = {
        listing: [
            {
                "path": "preview.json",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "start_date": "2024-02-01",
                "end_date": "2024-02-10",
            }
        ]
    }
    from packages.tradegrad_adapter.config import canonical_bytes

    result.dataset.content_sha256 = hashlib.sha256(
        canonical_bytes(result.dataset.manifest)
    ).hexdigest()
    return UUID(listing), path


def test_recorded_indicators_are_optional_hashed_immutable_evidence():
    result = synthetic_result()
    assert validate_result_bundle(result.bundle)
    payload = recorded_payload()
    bundle = with_section(result.bundle, payload)
    assert validate_result_bundle(bundle)
    assert bundle["indicators"]["points"][0]["value"] == payload["points"][0]["value"]
    payload["points"][0]["value"] = 999
    assert bundle["indicators"]["points"][0]["value"] != 999
    bundle["indicators"]["points"][0]["value"] = 1
    with pytest.raises(ValueError, match="hash or size"):
        validate_result_bundle(bundle)


@pytest.mark.parametrize(
    "mutation",
    [
        "instance",
        "key",
        "timestamp_units",
        "warmup",
        "value",
        "declaration",
        "duplicate",
        "precision",
        "points_budget",
        "unknown_section",
    ],
)
def test_rehashed_invalid_indicator_contract_is_rejected(mutation):
    bundle = with_section(synthetic_result().bundle)
    value = bundle["indicators"]
    if mutation == "instance":
        value["points"][0]["instance_id"] = "missing"
    elif mutation == "key":
        value["points"][0]["indicator_key"] = "guessed"
    elif mutation == "timestamp_units":
        value["points"][0]["timestamp_ms"] += 1
    elif mutation == "warmup":
        value["points"][0]["warmup"] = "false"
    elif mutation == "value":
        value["points"][0]["value"] = True
    elif mutation == "declaration":
        value["definitions"] = []
    elif mutation == "duplicate":
        value["instances"] *= 2
    elif mutation == "precision":
        value["definitions"][0]["precision"] = -1
    elif mutation == "points_budget":
        value["points"] *= 100001
    else:
        bundle["invented_indicators"] = []
    bundle["artifacts"] = [
        {
            "section": name,
            "media_type": "application/json",
            "sha256": hashlib.sha256(canonical_json_bytes(bundle[name])).hexdigest(),
            "bytes": len(canonical_json_bytes(bundle[name])),
        }
        for name in bundle_sections(bundle)
    ]
    with pytest.raises(ValueError):
        validate_result_bundle(bundle)


def test_exporter_never_guesses_missing_indicators_and_preserves_nulls():
    assert export_recorded_indicators(None)["status"] == "not_recorded"
    payload = recorded_payload()
    payload["points"][0]["value"] = float("nan")
    assert export_recorded_indicators(payload)["points"][0]["value"] is None
    payload["points"][0]["instance_id"] = "not declared"
    assert export_recorded_indicators(payload)["status"] == "unsupported"
    assert validate_indicators(export_recorded_indicators(payload))


def equity_bundle(rows, start="2024-01-01", end="2024-04-01", zone="UTC"):
    return {
        "engine": {"resolved_config": {"timezone": zone}},
        "equity": {"rows": rows},
        "window": {"start": start, "end": end},
    }


def test_monthly_formulas_partial_first_month_and_missing_month_are_explicit():
    bundle = equity_bundle(
        [
            ["2024-01-02T10:00:00Z", 100],
            ["2024-01-31T10:00:00Z", 110],
            ["2024-03-01T10:00:00Z", 121],
            ["2024-03-31T10:00:00Z", 130],
        ]
    )
    value = return_analysis(bundle)
    assert value.months[0].value == pytest.approx(0.1)
    assert value.months[0].status == "partial_period"
    assert value.months[1].status == "missing_observations"
    assert value.months[1].value is None
    assert value.months[2].status == "missing_observations"
    assert value.months[2].value is None
    assert value.valid_returns == 3  # explicitly inter-observation, not daily
    assert sum(item.count for item in value.bins) == 3
    assert value.coverage_status == "exchange_session_completeness_not_verified"


def test_full_observed_month_uses_preceding_close_and_clipped_end_is_partial():
    bundle = equity_bundle(
        [
            ["2024-01-31T10:00:00Z", 100],
            ["2024-02-01T10:00:00Z", 110],
            ["2024-02-29T10:00:00Z", 120],
            ["2024-03-01T10:00:00Z", 132],
        ],
        end="2024-03-02",
    )
    value = return_analysis(bundle)
    assert value.months[1].status == "observed_month"
    assert value.months[1].baseline_date == date(2024, 1, 31)
    assert value.months[1].value == pytest.approx(0.2)
    assert value.months[2].status == "partial_period"
    assert value.months[2].value == pytest.approx(0.1)


def test_return_denominators_nulls_intraday_timezone_and_singletons():
    rows = [
        ["2024-02-01T00:30:00Z", 100],
        ["2024-02-01T01:30:00Z", 101],
        ["2024-02-02T01:30:00Z", 0],
        ["2024-02-03T01:30:00Z", 5],
        ["2024-02-04T01:30:00Z", None],
    ]
    value = return_analysis(equity_bundle(rows, zone="America/New_York"))
    assert value.collapsed_intraday_observations == 1
    assert value.observations == 4
    assert value.valid_returns == 1 and value.excluded_returns == 2
    assert value.bins[0].lower == -1 and value.bins[0].upper == -1
    assert value.months[0].first_observation == date(2024, 1, 31)
    assert (
        return_analysis(equity_bundle(rows[:1])).status == "insufficient_observations"
    )


def test_analysis_does_not_mutate_metrics_reward_equity_or_benchmark():
    bundle = synthetic_result().bundle
    before = canonical_json_bytes(bundle)
    result = return_analysis(bundle)
    assert result.valid_returns == len(bundle["equity"]["rows"]) - 1
    assert result.months[0].value == pytest.approx(
        bundle["equity"]["rows"][-1][1] / bundle["equity"]["rows"][0][1] - 1
    )
    assert canonical_json_bytes(bundle) == before


def test_server_sort_filter_precedes_pagination_and_preserves_stable_indices():
    table = synthetic_result().bundle["fills"]
    table["rows"] = [table["rows"][0][:] for _ in range(5)]
    for index, row in enumerate(table["rows"]):
        row[0], row[3], row[5] = (
            str(index),
            "buy" if index % 2 == 0 else "sell",
            [3, 9, 3, 2, 1][index],
        )
    result = query_result_table(
        table,
        "fills",
        sort_by="price",
        sort_direction="desc",
        side="buy",
        offset=1,
        limit=1,
    )
    assert result.filtered_rows == 3 and result.total_rows == 5
    assert result.row_indices == [2]  # original-order tie breaker
    assert result.rows[0][5] == 3
    assert "price" in result.sortable_columns
    assert table["rows"][0][0] == "0"


@pytest.mark.parametrize(
    "section,kwargs",
    [
        ("fills", {"sort_by": "price; DROP TABLE users"}),
        ("fills", {"sort_by": "__class__"}),
        ("equity", {"sort_by": "timestamp"}),
        ("positions", {"side": "buy"}),
        ("orders", {"order_status": "anything"}),
        ("fills", {"order_status": "filled"}),
        ("fills", {"side": "BUY"}),
        ("fills", {"sort_direction": "desc"}),
        ("fills", {"limit": 501}),
    ],
)
def test_query_allowlist_rejects_unsupported_combinations(section, kwargs):
    with pytest.raises(DomainError):
        query_result_table(synthetic_result().bundle[section], section, **kwargs)


def test_replay_uses_verified_snapshot_prices_actual_fills_and_explicit_indicators(
    tmp_path,
):
    result = synthetic_result()
    listing, _path = stage_prices(result, tmp_path)
    with_section(result.bundle)
    value = price_replay(
        result, SimpleNamespace(storage_root=tmp_path), listing_id=listing, limit=1
    )
    assert value.bars[0].open == 100.123456789
    assert value.markers[0].price == 10.123456789  # never candle close
    assert value.markers[0].id == "fill:0"
    assert value.markers[0].on_price_bar
    assert value.bar_count == 2 and len(value.bars) == 1
    assert value.indicators[0].points[0].value == 9.123456789123
    assert value.indicators[0].metadata["indicator_key"] == "declared_signal"
    assert (
        price_replay(
            result,
            SimpleNamespace(storage_root=tmp_path),
            listing_id=listing,
            offset=99,
        ).markers
        == []
    )


def test_replay_missing_prices_never_replaced_and_wrong_listing_rejected(tmp_path):
    result = synthetic_result()
    listing, path = stage_prices(result, tmp_path, days=())
    replay = price_replay(
        result, SimpleNamespace(storage_root=tmp_path), listing_id=listing
    )
    assert not replay.bars and replay.markers[0].on_price_bar is False
    assert replay.indicators_status == "not_persisted"
    with pytest.raises(DomainError, match="immutable dataset"):
        price_replay(
            result, SimpleNamespace(storage_root=tmp_path), listing_id=UUID(int=999)
        )
    path.write_bytes(b"[] ")
    with pytest.raises(DomainError):
        price_replay(result, SimpleNamespace(storage_root=tmp_path), listing_id=listing)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start": date(2024, 1, 1)},
        {"end": date(2024, 3, 1)},
        {"limit": 501},
        {"offset": -1},
    ],
)
def test_replay_enforces_original_window_and_page_budget(tmp_path, kwargs):
    result = synthetic_result()
    listing, _ = stage_prices(result, tmp_path)
    with pytest.raises(DomainError):
        price_replay(
            result, SimpleNamespace(storage_root=tmp_path), listing_id=listing, **kwargs
        )


def test_indicators_do_not_fill_missing_days_and_last_recorded_session_value_wins(
    tmp_path,
):
    result = synthetic_result()
    listing, _ = stage_prices(result, tmp_path)
    payload = recorded_payload()
    warmup = copy.deepcopy(payload["points"][0])
    warmup["warmup"] = True
    last = copy.deepcopy(payload["points"][0])
    last["timestamp"] += 1_000_000
    last["timestamp_ms"] += 1
    last["value"] = None
    payload["points"] = [warmup, payload["points"][0], last]
    with_section(result.bundle, payload)
    value = price_replay(
        result, SimpleNamespace(storage_root=tmp_path), listing_id=listing
    )
    indicator = value.indicators[0]
    assert indicator.warmup_excluded == 1 and indicator.duplicate_session_points == 1
    assert len(indicator.points) == 1 and indicator.points[0].value is None


def test_pinned_calendar_identifies_missing_sessions_without_bridging_return_gaps():
    bundle = equity_bundle(
        [
            ["2024-02-01T10:00:00Z", 100],
            ["2024-02-05T10:00:00Z", 120],
            ["2024-02-06T10:00:00Z", 121],
        ],
        start="2024-02-01",
        end="2024-03-01",
    )
    manifest = {
        "evaluator_symbol_mapping": {"listing": "INDEX"},
        "coverage": [
            {
                "listing_id": "listing",
                "calendar_version": "frozen-test-calendar",
                "expected_sessions": [
                    "2024-02-01",
                    "2024-02-02",
                    "2024-02-05",
                    "2024-02-06",
                ],
            }
        ],
    }
    value = return_analysis(bundle, manifest)
    assert value.coverage_status == "missing_pinned_sessions"
    assert value.missing_sessions == [date(2024, 2, 2)]
    assert value.valid_returns == 1 and value.excluded_returns == 1
    assert (
        value.months[0].value is None
        and value.months[0].status == "missing_observations"
    )
    manifest["coverage"][0]["expected_sessions"].remove("2024-02-02")
    matched = return_analysis(bundle, manifest)
    assert matched.coverage_status == "matches_pinned_session_dates"
    assert matched.valid_returns == 2
    manifest["coverage"][0]["calendar_version"] = None
    assert (
        return_analysis(bundle, manifest).coverage_status
        == "exchange_session_completeness_not_verified"
    )


def test_pinned_calendar_needs_all_staged_listings_and_flags_unexpected_dates():
    bundle = equity_bundle(
        [["2024-02-01T10:00:00Z", 100], ["2024-02-04T10:00:00Z", 120]],
        start="2024-02-01",
        end="2024-03-01",
    )
    manifest = {
        "evaluator_symbol_mapping": {"one": "A", "two": "B"},
        "coverage": [
            {
                "listing_id": "one",
                "calendar_version": "frozen",
                "expected_sessions": ["2024-02-01", "2024-02-02"],
            }
        ],
    }
    assert return_analysis(bundle, manifest).expected_sessions is None
    manifest["evaluator_symbol_mapping"].pop("two")
    value = return_analysis(bundle, manifest)
    assert value.unexpected_observations == [date(2024, 2, 4)]
    assert value.valid_returns == 0 and value.months[0].value is None


def test_benchmark_attachment_preserves_hashed_recorded_indicator_extension():
    from packages.tradegrad_adapter.benchmarks import attach_comparison
    from tests.tradegrad.test_benchmark_comparisons import prepared

    bundle = with_section(synthetic_result().bundle)
    days = [row[0][:10] for row in bundle["equity"]["rows"]]
    attached = attach_comparison(bundle, prepared(days))
    assert validate_result_bundle(attached)
    assert attached["indicators"] == bundle["indicators"]
    assert attached["metrics"] == bundle["metrics"]
    assert attached["legacy_performance"] == bundle["legacy_performance"]


def test_missing_price_sessions_are_kept_in_page_partition_not_snapped(tmp_path):
    result = synthetic_result()
    listing, _ = stage_prices(result, tmp_path, days=("2024-02-02", "2024-02-05"))
    first = price_replay(
        result, SimpleNamespace(storage_root=tmp_path), listing_id=listing, limit=1
    )
    assert first.markers[0].session_date == date(2024, 2, 1)
    assert first.markers[0].on_price_bar is False
    assert (
        price_replay(
            result,
            SimpleNamespace(storage_root=tmp_path),
            listing_id=listing,
            offset=1,
            limit=1,
        ).markers
        == []
    )


def test_replay_marker_limit_is_explicit_not_a_false_complete_history(tmp_path):
    result = synthetic_result()
    listing, _ = stage_prices(result, tmp_path)
    result.bundle["fills"]["rows"] = [
        result.bundle["fills"]["rows"][0][:] for _ in range(2001)
    ]
    value = price_replay(
        result, SimpleNamespace(storage_root=tmp_path), listing_id=listing
    )
    assert (
        value.marker_count == 2001
        and len(value.markers) == 2000
        and value.markers_truncated
    )


def test_replay_scan_budget_is_enforced_before_unbounded_projection(
    tmp_path, monkeypatch
):
    import backend.app.result_exploration as exploration

    result = synthetic_result()
    listing, _ = stage_prices(result, tmp_path)
    monkeypatch.setattr(exploration, "MAX_REPLAY_BYTES", 1)
    with pytest.raises(DomainError, match="scan budget"):
        price_replay(result, SimpleNamespace(storage_root=tmp_path), listing_id=listing)


def test_declared_unsupported_indicator_renderer_keeps_observations_visible(tmp_path):
    result = synthetic_result()
    listing, _ = stage_prices(result, tmp_path)
    payload = recorded_payload()
    payload["definitions"][0]["render_type"] = "scatter"
    with_section(result.bundle, payload)
    value = price_replay(
        result, SimpleNamespace(storage_root=tmp_path), listing_id=listing
    )
    assert value.indicators[0].status == "unsupported_render"
    assert value.indicators[0].points[0].value == payload["points"][0]["value"]
