"""Pure projection and SQL expression checks; not engine/DB/browser evidence."""

import copy
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from backend.app.list_queries import candidate_query, listing_query
from backend.app.portable_result_tables import query_portable_table
from backend.app.result_exploration import (
    exploration_context,
    price_replay,
    result_return_analysis,
)
from backend.app.services import DomainError
from backend.tests.final_result_fixtures import synthetic_final_result
from backend.tests.research_view_fixtures import synthetic_result
from backend.tests.test_result_exploration import stage_prices


def portable_projection(final=False):
    """Explicit synthetic projection input, not a fully admitted engine artifact."""
    result = synthetic_result()
    listing = next(iter(result.dataset.manifest["evaluator_symbol_mapping"]))
    rows = copy.deepcopy(result.bundle["equity"]["rows"])
    result.bundle.clear()
    result.bundle.update(
        evaluation_mode="portable",
        result_trust="isolated_policy_trusted_akquant_engine",
        ledger_origin="trusted_engine",
        manifest={"timezone": "Asia/Shanghai"},
        identity={"market_data_sha256": "e" * 64},
        window={"start": "2024-02-01", "end": "2024-02-10", "interval": "half_open"},
        account_series=[{"native_at": at, "equity": equity} for at, equity in rows],
        fills=[
            {
                "listing_id": listing,
                "fill_id": "trusted-fill-01",
                "order_id": "trusted-order-01",
                "native_at": "2024-02-01T07:00:00+00:00",
                "native_timestamp_ns": "1706770800000000000",
                "economic_reference_time": "2024-02-01T01:30:00+00:00",
                "economic_reference": "session_open_with_native_matching_and_slippage",
                "side": "buy",
                "quantity": 100,
                "price": 10.555,
                "commission": 0.3,
            }
        ],
        decision_trace=[{"equity": 1e100, "indicator": 9e99}],
        final_state={"equity": -1e100},
        summary={"original": 42},
    )
    result.run.config = {
        "schema_version": "tradegrad.portable.final-evaluation.v1"
        if final
        else "tradegrad.portable.run.v1"
    }
    if final:
        result.run.config["selection_id"] = str(uuid4())
        result.evaluation.window["split"] = "holdout"
    return result


@pytest.mark.parametrize(
    "mode", ["native_research", "native_final", "portable_research", "portable_final"]
)
def test_mode_context_preserves_artifact_window_and_holdout_identity(mode):
    result = (
        portable_projection(mode.endswith("final"))
        if mode.startswith("portable")
        else synthetic_final_result()
        if mode.endswith("final")
        else synthetic_result()
    )
    before = copy.deepcopy(result.bundle)
    view = result_return_analysis(result)
    assert view.context.mode == mode
    assert str(view.context.evaluation_id) == str(result.evaluation.id)
    assert view.context.artifact_sha256 == result.artifact.sha256
    assert view.context.dataset_sha256 == result.dataset.content_sha256
    assert (view.context.split == "holdout") == mode.endswith("final")
    assert bool(view.context.selection_id) == mode.endswith("final")
    assert result.bundle == before
    if mode.startswith("portable"):
        assert view.context.evaluation_data_sha256 == "e" * 64
        assert view.context.ledger_origin == "trusted_engine"
        assert not any("same-process" in warning for warning in view.warnings)


@pytest.mark.parametrize("change", ["mode", "holdout", "selection"])
def test_cross_mode_and_unbound_final_projection_fail_closed(change):
    result = portable_projection()
    if change == "mode":
        result.run.config["schema_version"] = "tradegrad.run.v1"
    elif change == "holdout":
        result.evaluation.window["split"] = "holdout"
    else:
        result.run.config["selection_id"] = str(uuid4())
    with pytest.raises(DomainError):
        exploration_context(result)


@pytest.mark.parametrize("final", [False, True])
def test_portable_replay_is_bound_prices_and_trusted_fills_not_policy_state(
    tmp_path, final
):
    result = portable_projection(final)
    listing, path = stage_prices(result, tmp_path)
    original = path.read_bytes()
    replay = price_replay(
        result, SimpleNamespace(storage_root=tmp_path), listing_id=listing
    )
    assert replay.bars[0].open == 100.123456789
    assert replay.markers[0].price == 10.555
    assert replay.markers[0].fill_id == "trusted-fill-01"
    assert replay.markers[0].native_timestamp_ns == "1706770800000000000"
    assert replay.markers[0].timestamp != replay.markers[0].economic_reference_time
    assert replay.markers[0].id == "fill:0" and replay.markers[0].on_price_bar
    assert replay.indicators_status == "not_persisted" and replay.indicators == []
    assert "Policy state" in replay.indicators_reason
    assert path.read_bytes() == original


def test_portable_filters_sort_before_page_and_keep_artifact_row_identity():
    bundle = portable_projection().bundle
    original = bundle["fills"][0]
    bundle["fills"] = [
        {**original, "price": price, "side": side}
        for price, side in [(5, "sell"), (9, "buy"), (7, "buy"), (9, "buy")]
    ]
    result = query_portable_table(
        bundle, "fills", 1, 1, side="buy", sort_by="price", sort_direction="desc"
    )
    assert result["count"] == 3 and result["total_rows"] == 4
    assert result["row_indices"] == [3] and result["rows"][0]["price"] == 9
    assert query_portable_table(bundle, "fills", start=date(2024, 2, 2))["count"] == 0


@pytest.mark.parametrize(
    "section,query",
    [
        ("fills", {"sort_by": "policy.equity"}),
        ("fills", {"sort_direction": "desc"}),
        ("account_series", {"sort_by": "equity"}),
        ("decision_trace", {"listing_id": uuid4()}),
        ("fills", {"side": "short"}),
        ("native_metrics", {"start": date(2024, 2, 1)}),
        ("fills", {"limit": 501}),
        ("fills", {"offset": 100001}),
    ],
)
def test_portable_table_allowlist_rejects_unsupported_inputs(section, query):
    with pytest.raises(DomainError):
        query_portable_table(portable_projection().bundle, section, **query)


@pytest.mark.parametrize(
    "factory,identifier", [(candidate_query, uuid4()), (listing_query, uuid4())]
)
def test_list_queries_bind_literal_text_and_keep_stable_id_order(factory, identifier):
    value = "x%' OR 1=1 --_"
    query = factory(identifier, search=value, sort_direction="desc")
    compiled = query.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert value not in sql and identifier in compiled.params.values()
    assert "ORDER BY" in sql and "id ASC" in sql and "DESC" in sql
    assert any(
        isinstance(item, str) and "/%" in item and "/_" in item
        for item in compiled.params.values()
    )
    if factory is listing_query:
        assert "owner_listings.owner_id" in sql
    else:
        assert "research_candidates.run_id" in sql


@pytest.mark.parametrize(
    "factory,query",
    [
        (candidate_query, {"sort_by": "code; DROP TABLE users"}),
        (candidate_query, {"kind": "best"}),
        (candidate_query, {"min_iteration": -1}),
        (listing_query, {"sort_by": "owner_id"}),
        (listing_query, {"sort_direction": "random"}),
        (listing_query, {"search": "x" * 129}),
        (listing_query, {"mic": "INVALID"}),
    ],
)
def test_named_query_allowlists_reject_unbounded_or_dynamic_inputs(factory, query):
    with pytest.raises(DomainError):
        factory(uuid4(), **query)


def test_preview_metadata_checksum_is_verified_before_any_file_read(
    tmp_path, monkeypatch
):
    from backend.app import result_exploration

    result = portable_projection()
    listing, _ = stage_prices(result, tmp_path)
    result.dataset.manifest["preview_files"][str(listing)][0]["sha256"] = "f" * 64
    monkeypatch.setattr(
        result_exploration,
        "read_verified",
        lambda *args: pytest.fail("Unbound preview file read"),
    )
    with pytest.raises(DomainError, match="pinned checksum"):
        price_replay(result, SimpleNamespace(storage_root=tmp_path), listing_id=listing)


def test_portable_navigation_is_chronological_but_retains_original_fill_indices(
    tmp_path,
):
    result = portable_projection()
    listing, _ = stage_prices(result, tmp_path)
    original = result.bundle["fills"][0]
    result.bundle["fills"] = [
        {**original, "native_at": "2024-02-02T07:00:00+00:00"},
        original,
    ]
    replay = price_replay(
        result, SimpleNamespace(storage_root=tmp_path), listing_id=listing
    )
    assert [marker.id for marker in replay.markers] == ["fill:1", "fill:0"]
