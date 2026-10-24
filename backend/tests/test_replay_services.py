"""Synthetic pure service contracts with real immutable Parquet/CSV/JSON reads.

These tests do not claim database admission or runtime execution certification.
"""

import hashlib
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from backend.app.replay_services import (
    replay_execution,
    replay_instruments,
    replay_market,
)
from backend.app.services import DomainError
from backend.app.trace_schemas import TraceContext
from backend.app.trace_services import OwnedTrace, candidates_view, decision_view
from backend.app.workbench_schemas import WorkbenchSourceAssociation
from backend.tests.research_view_fixtures import synthetic_result
from backend.tests.test_result_exploration import stage_prices
from packages.research_trace.contracts import (
    CaptureConfig,
    EvaluationBinding,
    canonical_bytes,
)
from packages.research_trace.recorder import Recorder
from packages.research_trace.storage import TraceReader, write_trace_artifacts

SOURCE = WorkbenchSourceAssociation(
    candidate_id=None,
    candidate_basis="unavailable",
    strategy_version_id=None,
    strategy_basis="unavailable",
    missing_reasons=["Synthetic service identity only."],
)


def rehash(result):
    result.dataset.content_sha256 = hashlib.sha256(
        canonical_bytes(result.dataset.manifest)
    ).hexdigest()


def universe(result, root, *, state="user_fixed", snapshots=None):
    result.dataset.storage_key = result.dataset.storage_key or "frozen"
    mapping = result.dataset.manifest["evaluator_symbol_mapping"]
    snapshots = snapshots or {"2024-01-01": list(mapping.values())}
    refs = {}
    for day, symbols in snapshots.items():
        name = f"universe/members_list_{day}.csv"
        raw = (
            "updateDate,code,code_name\n"
            + "".join(f"{day},{symbol},{symbol}\n" for symbol in symbols)
        ).encode()
        path = root / result.dataset.storage_key / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        refs[name] = hashlib.sha256(raw).hexdigest()
    ref = result.run.config.get("source_config", result.run.config)["universe"]
    result.dataset.manifest.update(
        files=refs,
        universe={
            "id": ref["id"],
            "content_sha256": ref["sha256"],
            "state": state,
            "as_of": "2024-01-01T00:00:00+00:00",
        },
    )
    rehash(result)


def observed_trace(
    tmp_path,
    result=None,
    *,
    links=None,
    second=False,
    generic=False,
    day=1,
    stage_observations=None,
):
    result = result or synthetic_result()
    recorder = Recorder(
        code_sha256=result.evaluation.code_sha256, config=CaptureConfig(mode="research")
    )
    for seq in range(2 if second else 1):
        recorder.begin_decision(
            data_event_id="native_order:order-1" if generic else "frozen:event-1",
            event_seq=seq,
            scope="portfolio",
            callback_phase="native.execution_result" if generic else "on_bar",
            information_cutoff=datetime(2024, 2, day, 2, tzinfo=UTC),
            decision_time=datetime(2024, 2, day, 2, tzinfo=UTC),
            market_timezone="Asia/Shanghai",
        )
        recorder.emit(
            "execution", {"order_links": links or [], **(stage_observations or {})}
        )
        recorder.end_decision()
    binding = EvaluationBinding(
        run_id=result.run.id,
        execution_attempt_id=result.evaluation.attempt_id,
        evaluation_id=result.evaluation.id,
        candidate_id=None,
        strategy_version_id=result.run.strategy_version_id,
        code_sha256=result.evaluation.code_sha256,
        dataset_version_id=result.dataset.id,
        dataset_sha256=result.dataset.content_sha256,
        universe_version_id=None,
        universe_sha256=None,
        execution_config_sha256="c" * 64,
        engine_dependencies_sha256="d" * 64,
        result_manifest_sha256=result.artifact.sha256,
    )
    manifest, artifacts = write_trace_artifacts(
        recorder.records, binding, recorder.summary(), tmp_path / "trace"
    )
    context = TraceContext(
        evaluation_id=result.evaluation.id,
        execution_attempt_id=result.evaluation.attempt_id,
        result_manifest_sha256=result.artifact.sha256,
        trace_manifest_sha256=artifacts[-1].sha256,
        status="available",
        binding=binding,
    )
    reader = TraceReader(manifest, {item.name: item.path for item in artifacts})
    return OwnedTrace(result, context, manifest, reader), recorder.records[
        0
    ].decision_id


def link(**updates):
    return {
        "logical_order_id": "intent-1",
        "child_order_ids": ["order-1"],
        "fill_ids": [],
        "instrument_id": "target_symbol",
        "submitted_at": "2024-02-01T02:00:00+00:00",
        "fill_times": [],
        "status": "accepted",
        "source": "strategy_emitted",
        **updates,
    }


@pytest.fixture
def settings(tmp_path):
    return SimpleNamespace(storage_root=tmp_path)


def test_time_alias_frozen_prices_and_cutoff_are_separate(tmp_path, settings):
    result = synthetic_result()
    listing, _ = stage_prices(result, tmp_path)
    universe(result, tmp_path)
    trace, decision = observed_trace(tmp_path, result)
    page = replay_instruments(trace, settings, SOURCE)
    assert page.items[0].instrument_id == "target_symbol"
    assert page.items[0].listing_id == listing
    assert page.items[0].configured_evaluation_member is True
    assert page.market.historical_membership_status == "user_fixed_assumption"
    market = replay_market(
        trace, settings, decision, instrument_id="target_symbol", limit=1
    )
    assert market.status == "available" and market.total == 2
    assert len(market.bars) == 1 and market.bars[0].close == 101.0
    assert market.information_cutoff == datetime(2024, 2, 1, 2, tzinfo=UTC)
    assert market.market.dataset_sha256 == result.dataset.content_sha256
    assert not hasattr(market, "indicators")
    assert decision_view(trace, decision).decision.session_date == date(2024, 2, 1)
    later = replay_market(
        trace, settings, decision, instrument_id="target_symbol", offset=1
    )
    assert later.bars[0].session_date > later.information_cutoff.date()
    assert "retrospective" in later.market.warnings[0]


@pytest.mark.parametrize(
    "state,expected",
    [
        ("current_snapshot", "current_snapshot_only"),
        ("historical_membership", "historical_intervals_declared"),
        ("point_in_time_verified", "point_in_time_evidence_declared"),
    ],
)
def test_universe_capability_is_actual_declared_state_not_pit_proof(
    tmp_path, settings, state, expected
):
    result = synthetic_result()
    universe(result, tmp_path, state=state)
    trace, _ = observed_trace(tmp_path, result)
    market = replay_instruments(trace, settings, SOURCE).market
    assert market.historical_membership_status == expected
    assert "not independent" in market.warnings[1]


def test_historical_mapping_distinguishes_staged_union_from_effective_session(
    tmp_path, settings
):
    result = synthetic_result()
    result.bundle["identity"]["task"] = "cn_cross"
    ids = [str(uuid4()), str(uuid4()), str(uuid4())]
    result.dataset.manifest["evaluator_symbol_mapping"] = dict(
        zip(ids, ["A", "B", "FUTURE"], strict=True)
    )
    universe(
        result,
        tmp_path,
        state="historical_membership",
        snapshots={"2024-01-01": ["A"], "2024-02-03": ["B"], "2024-02-15": ["FUTURE"]},
    )
    trace, decision = observed_trace(tmp_path, result)
    page = replay_instruments(trace, settings, SOURCE, session_date=date(2024, 2, 1))
    observed = {row.symbol: row for row in page.items}
    assert (
        observed["B"].configured_evaluation_member,
        observed["B"].configured_session_member,
    ) == (True, False)
    assert observed["FUTURE"].configured_evaluation_member is False
    outside = replay_market(trace, settings, decision, instrument_id="B")
    assert outside.status == "outside_configured_universe" and outside.bars is None
    assert "genuine point-in-time" in outside.missing_reason
    assert (
        replay_market(trace, settings, decision, instrument_id="not-a-member").status
        == "outside_configured_universe"
    )


def test_unknown_membership_unmapped_prices_and_no_rows_stay_distinct(
    tmp_path, settings
):
    result = synthetic_result()
    trace, decision = observed_trace(tmp_path, result)
    instruments = replay_instruments(trace, settings, SOURCE)
    assert instruments.items[0].configured_evaluation_member is None
    assert instruments.market.membership_status == "not_collected"
    assert (
        replay_market(trace, settings, decision, instrument_id="unknown").status
        == "unsupported"
    )
    no_preview = replay_market(trace, settings, decision, instrument_id="target_symbol")
    assert (
        no_preview.status == "not_collected"
        and no_preview.bars is None
        and no_preview.total is None
    )
    stage_prices(result, tmp_path)
    empty = replay_market(
        trace,
        settings,
        decision,
        instrument_id="target_symbol",
        start=date(2024, 2, 5),
        end=date(2024, 2, 6),
    )
    assert empty.status == "no_rows" and empty.bars == [] and empty.total == 0


@pytest.mark.parametrize("fault", ["universe", "price"])
def test_replay_checksum_tamper_never_falls_back_to_fills(tmp_path, settings, fault):
    result = synthetic_result()
    _, price_path = stage_prices(result, tmp_path)
    universe(result, tmp_path)
    trace, decision = observed_trace(tmp_path, result)
    path = (
        price_path
        if fault == "price"
        else next((tmp_path / result.dataset.storage_key / "universe").glob("*.csv"))
    )
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(DomainError, match="checksum"):
        replay_market(trace, settings, decision, instrument_id="target_symbol")


def test_execution_uses_exact_child_id_for_multiple_later_fills_not_symbol_or_time(
    tmp_path,
):
    result = synthetic_result()
    fills = result.bundle["fills"]["rows"]
    fills[0][0] = "fill-1"
    fills.append(["fill-2", *fills[0][1:]])
    fills.append(["unrelated", "other-order", *fills[0][2:]])
    trace, decision = observed_trace(tmp_path, result, links=[link()], second=True)
    page = replay_execution(trace, decision)
    assert [row.record_id for row in page.items] == [
        "intent-1",
        "order-1",
        "fill-1",
        "fill-2",
    ]
    assert page.items[0].status == "not_collected"
    assert page.items[1].record["status"] == "filled"
    assert page.items[1].link.status == "accepted"
    assert page.items[-1].association_basis == "child_order_id"
    assert page.items[-1].link.fill_ids == []
    assert (
        replay_execution(trace, decision, offset=2, limit=1).items[0].record_id
        == "fill-1"
    )
    assert (
        replay_execution(trace, decision, instrument_id="not-selected").status
        == "no_linked_records"
    )
    assert replay_execution(trace, decision, stage_id="other-stage").total == 0
    assert (
        replay_execution(trace, decision, logical_order_id="another-intent").total == 0
    )


@pytest.mark.parametrize(
    "status,quantity,filled",
    [("rejected", 10, 0), ("partially_filled", 10, 3), ("accepted", 10, 0)],
)
def test_rejection_partial_unfilled_records_preserve_actual_economics(
    tmp_path, status, quantity, filled
):
    result = synthetic_result()
    order = result.bundle["orders"]["rows"][0]
    order[3:5] = [quantity, filled]
    order[8:10] = [status, "risk: insufficient cash" if status == "rejected" else None]
    result.bundle["fills"]["rows"] = []
    trace, decision = observed_trace(tmp_path, result, links=[link()])
    page = replay_execution(trace, decision)
    record = next(
        item.record for item in page.items if item.record_kind == "child_order"
    )
    assert (record["quantity"], record["filled_quantity"], record["status"]) == (
        quantity,
        filled,
        status,
    )
    assert record["reject_reason"] == order[9]
    assert page.total == 2  # No synthetic zero-quantity fill or no-intent record.


def test_generic_execution_remains_explicitly_unpaired(tmp_path):
    trace, decision = observed_trace(
        tmp_path,
        generic=True,
        links=[
            link(logical_order_id="observed-native:order-1", source="engine_observed")
        ],
    )
    page = replay_execution(trace, decision)
    assert all(
        row.association_basis == "unpaired_native_execution" for row in page.items
    )
    assert "no recovered logical" in page.warnings[-1]


@pytest.mark.parametrize("mutation", ["symbol", "fill_parent", "duplicate"])
def test_conflicting_ids_fail_closed_without_time_symbol_guess(tmp_path, mutation):
    result = synthetic_result()
    links = [link(fill_ids=[str(result.bundle["fills"]["rows"][0][0])])]
    if mutation == "symbol":
        result.bundle["orders"]["rows"][0][1] = "different"
    elif mutation == "fill_parent":
        result.bundle["fills"]["rows"][0][1] = "different"
    else:
        result.bundle["orders"]["rows"].append(
            result.bundle["orders"]["rows"][0].copy()
        )
    trace, decision = observed_trace(tmp_path, result, links=links)
    with pytest.raises(DomainError) as error:
        replay_execution(trace, decision)
    assert error.value.code == "trace_integrity"


def test_missing_linked_row_is_not_collected_observation_or_empty_array(tmp_path):
    trace, decision = observed_trace(
        tmp_path, links=[link(child_order_ids=["not-present"])]
    )
    page = replay_execution(trace, decision)
    assert page.items[1].status == "no_matching_record"
    assert page.items[1].record is None
    assert page.items[1].record_index is None


def test_absent_trace_keeps_nulls_and_missing_reason(tmp_path, settings):
    trace, _ = observed_trace(tmp_path)
    missing = OwnedTrace(
        trace.result,
        trace.context.model_copy(
            update={"status": "not_collected", "missing_reason": "Capture was off."}
        ),
        None,
        None,
    )
    result = replay_execution(missing, "not-reconstructed")
    assert (
        result.items is None
        and result.total is None
        and result.missing_reason == "Capture was off."
    )
    market = replay_market(
        missing, settings, "not-reconstructed", instrument_id="target_symbol"
    )
    assert market.bars is None and market.information_cutoff is None


@pytest.mark.parametrize(
    "query",
    [{"limit": 501}, {"offset": 100001}, {"limit": 0}, {"offset": -1}, {"limit": True}],
)
def test_pages_are_bounded(tmp_path, query):
    trace, decision = observed_trace(tmp_path)
    with pytest.raises(DomainError) as error:
        replay_execution(trace, decision, **query)
    assert error.value.status_code == 422


def test_response_byte_budget_is_enforced(tmp_path, monkeypatch):
    from backend.app import replay_services

    trace, decision = observed_trace(tmp_path, links=[link()])
    monkeypatch.setattr(replay_services, "MAX_RESPONSE_BYTES", 200)
    with pytest.raises(DomainError) as error:
        replay_execution(trace, decision)
    assert error.value.code == "replay_detail_too_large"


def test_portable_logical_child_fill_chain_uses_listing_ids(tmp_path):
    result = synthetic_result()
    listing = str(uuid4())
    result.bundle.update(
        evaluation_mode="portable",
        logical_orders=[
            {
                "logical_order_id": "intent-1",
                "listing_id": listing,
                "native_order_ids": ["order-1"],
                "quantity": 10,
                "filled_quantity": 3,
            }
        ],
        orders=[
            {
                "order_id": "order-1",
                "logical_order_id": "intent-1",
                "listing_id": listing,
                "quantity": 10,
                "filled_quantity": 3,
                "status": "partially_filled",
            }
        ],
        fills=[
            {
                "fill_id": "fill-1",
                "order_id": "order-1",
                "logical_order_id": "intent-1",
                "listing_id": listing,
                "quantity": 3,
                "price": 10,
            }
        ],
    )
    trace, decision = observed_trace(
        tmp_path, result, links=[link(instrument_id=listing, source="adapter_observed")]
    )
    page = replay_execution(trace, decision)
    assert [row.record_kind for row in page.items] == [
        "logical_order",
        "child_order",
        "fill",
    ]
    assert all(row.status == "available" for row in page.items)
    assert page.items[0].record["filled_quantity"] == 3
    assert page.items[-1].record["listing_id"] == listing


def test_execution_observation_after_window_keeps_original_window(tmp_path, settings):
    result = synthetic_result()
    universe(result, tmp_path)
    stage_prices(result, tmp_path)
    trace, decision = observed_trace(tmp_path, result, day=10)
    market = replay_market(trace, settings, decision, instrument_id="target_symbol")
    assert market.status == "available"
    assert market.session_date == date(2024, 2, 10)
    assert market.market.window_end == date(2024, 2, 10)
    assert market.instrument.configured_session_member is None


@pytest.mark.parametrize(
    "mode", ["native_research", "native_final", "portable_research", "portable_final"]
)
def test_all_modes_keep_frozen_market_identity_and_actual_instrument_mapping(
    tmp_path, settings, mode
):
    result = synthetic_result()
    source = result.run.config.copy()
    portable, final = mode.startswith("portable"), mode.endswith("final")
    schema = (
        "tradegrad."
        + ("portable." if portable else "")
        + ("final-evaluation" if final else "run")
        + ".v1"
    )
    if final:
        result.run.config = {
            "schema_version": schema,
            "source_config": source,
            "selection_id": str(uuid4()),
        }
        result.evaluation.window = {**result.evaluation.window, "split": "holdout"}
    else:
        result.run.config = {**source, "schema_version": schema}
    listing = next(iter(result.dataset.manifest["evaluator_symbol_mapping"]))
    result.dataset.manifest["listings"] = [{"listing_id": listing}]
    if portable:
        result.bundle.update(
            evaluation_mode="portable",
            manifest={"timezone": "Asia/Shanghai"},
            ledger_origin="trusted_engine",
        )
    stage_prices(result, tmp_path)
    universe(result, tmp_path)
    trace, decision = observed_trace(tmp_path, result)
    instruments = replay_instruments(trace, settings, SOURCE)
    item = instruments.items[0]
    assert item.instrument_id == (listing if portable else "target_symbol")
    assert (
        item.configured_evaluation_member is True
        and item.configured_session_member is True
    )
    assert instruments.market.ledger_origin == (
        "trusted_engine" if portable else "native_same_process"
    )
    assert instruments.market.universe_version_id == UUID(source["universe"]["id"])
    market = replay_market(trace, settings, decision, instrument_id=item.instrument_id)
    assert market.status == "available" and market.instrument.listing_id == UUID(
        listing
    )
    assert market.market.dataset_sha256 == result.dataset.content_sha256


def test_timing_alias_uses_actual_staged_listing_not_an_inactive_dataset_row(
    tmp_path, settings
):
    result = synthetic_result()
    listing, _ = stage_prices(result, tmp_path)
    result.dataset.manifest["evaluator_symbol_mapping"][str(uuid4())] = "FUTURE"
    universe(
        result,
        tmp_path,
        state="historical_membership",
        snapshots={"2024-01-01": ["INDEX"], "2024-02-15": ["FUTURE"]},
    )
    trace, decision = observed_trace(tmp_path, result)
    market = replay_market(trace, settings, decision, instrument_id="target_symbol")
    assert market.status == "available" and market.instrument.listing_id == listing
    assert (
        replay_market(trace, settings, decision, instrument_id="FUTURE").status
        == "outside_configured_universe"
    )


def test_registered_off_manifest_keeps_real_pins_while_details_are_uncollected(
    tmp_path, settings
):
    trace, _ = observed_trace(tmp_path)
    recorder = Recorder(
        code_sha256=trace.context.binding.code_sha256, config=CaptureConfig(mode="off")
    )
    manifest, artifacts = write_trace_artifacts(
        [], trace.context.binding, recorder.summary(), tmp_path / "off"
    )
    context = trace.context.model_copy(
        update={
            "status": "not_collected",
            "trace_manifest_sha256": artifacts[-1].sha256,
            "missing_reason": "; ".join(manifest.missing_reasons),
        }
    )
    frozen_off = OwnedTrace(trace.result, context, manifest, None)
    for response in [
        replay_execution(frozen_off, "unobserved"),
        replay_market(
            frozen_off, settings, "unobserved", instrument_id="target_symbol"
        ),
    ]:
        assert response.context.trace_manifest_sha256 == artifacts[-1].sha256
        assert response.context.binding == trace.context.binding
        assert response.status == "not_collected" and response.missing_reason
        assert response.total is None
    assert manifest.capture.mode == "off" and manifest.decisions_saved == 0


def duplicate_observations(code_hash, cutoff):
    """Valid duplicate local IDs with distinct immutable observation identities."""
    rows = {"features": [], "predicates": [], "candidates": []}
    for ordinal, (instrument, value) in enumerate(
        [("other", -1), ("target_symbol", 0), ("target_symbol", 3)]
    ):
        source = {
            "code_sha256": code_hash,
            "file": "strategy.py",
            "symbol": "Strategy.on_bar",
            "line_start": 10 + ordinal,
            "line_end": 10 + ordinal,
        }
        rows["features"].append(
            {
                "feature_id": "repeated",
                "instrument_id": instrument,
                "value": value,
                "validity": "valid",
                "unit": "count",
                "calculation_window": "recorded current event",
                "information_cutoff": cutoff,
                "source_ref": source,
            }
        )
        rows["predicates"].append(
            {
                "condition_id": "repeated",
                "instrument_id": instrument,
                "operator": "gt",
                "operands": [value],
                "threshold": 0,
                "state": "true" if value > 0 else "false",
                "source_ref": source,
            }
        )
        rows["candidates"].append(
            {
                "instrument_id": instrument,
                "universe_member": True,
                "eligibility": "eligible",
                "reason": "selected" if value > 0 else "rank_not_selected",
                "score": value,
                "score_validity": "valid",
                "selected": value > 0,
                "prior_holding": False,
                "source_ref": source,
            }
        )
    return rows


def test_duplicate_observation_ids_keep_exact_saved_ordinal_across_pages_and_filters(
    tmp_path,
):
    result = synthetic_result()
    observations = duplicate_observations(
        result.evaluation.code_sha256, datetime(2024, 2, 1, 2, tzinfo=UTC)
    )
    trace, decision = observed_trace(tmp_path, result, stage_observations=observations)
    pages = [
        decision_view(
            trace,
            decision,
            stage_id="execution",
            instrument_id="target_symbol",
            offset=offset,
            limit=1,
        )
        for offset in (0, 1)
    ]
    for table in ("features", "predicates"):
        first, second = [getattr(page, table).items[0] for page in pages]
        assert (first.ordinal, second.ordinal) == (1, 2)
        assert first.stage_id == second.stage_id == "execution"
        assert (
            first.observation.instrument_id
            == second.observation.instrument_id
            == "target_symbol"
        )
        assert first.observation.source_ref.line_start == 11
        assert second.observation.source_ref.line_start == 12
        assert (
            first.observation.source_ref.code_sha256
            == second.observation.source_ref.code_sha256
            == result.evaluation.code_sha256
        )
        assert all(getattr(page, table).total == 2 for page in pages)
    assert [page.features.items[0].observation.value for page in pages] == [0, 3]
    assert [page.predicates.items[0].observation.state for page in pages] == [
        "false",
        "true",
    ]
    assert [page.predicates.items[0].observation.operands for page in pages] == [
        [0],
        [3],
    ]
    candidates = [
        candidates_view(
            trace,
            decision,
            stage_id="execution",
            instrument_id="target_symbol",
            offset=offset,
            limit=1,
        )
        for offset in (0, 1)
    ]
    assert [page.items[0].ordinal for page in candidates] == [1, 2]
    assert [page.items[0].observation.score for page in candidates] == [0, 3]
    assert [page.items[0].observation.source_ref.line_start for page in candidates] == [
        11,
        12,
    ]
    selected = candidates_view(trace, decision, score_min=1)
    assert selected.total == 1 and selected.items[0].ordinal == 2


def test_exact_ordinal_selects_duplicate_without_previous_page_or_filter_state(
    tmp_path,
):
    result = synthetic_result()
    trace, decision = observed_trace(
        tmp_path,
        result,
        stage_observations=duplicate_observations(
            result.evaluation.code_sha256, datetime(2024, 2, 1, 2, tzinfo=UTC)
        ),
    )
    detail = decision_view(
        trace,
        decision,
        stage_id="execution",
        instrument_id="target_symbol",
        record_ordinal=2,
    )
    assert detail.features.total == detail.predicates.total == 1
    assert detail.features.items[0].ordinal == detail.predicates.items[0].ordinal == 2
    assert detail.features.items[0].observation.value == 3
    assert detail.predicates.items[0].observation.state == "true"
    assert detail.features.items[0].observation.source_ref.line_start == 12
    candidate = candidates_view(
        trace,
        decision,
        stage_id="execution",
        instrument_id="target_symbol",
        record_ordinal=2,
    )
    assert candidate.total == 1 and candidate.items[0].ordinal == 2
    assert candidate.items[0].observation.score == 3
    for request in (decision_view, candidates_view):
        absent = request(
            trace,
            decision,
            stage_id="execution",
            instrument_id="other",
            record_ordinal=2,
        )
        page = absent.features if request is decision_view else absent
        assert page.total == 0 and page.items == []


@pytest.mark.parametrize(
    "query",
    [
        {"record_ordinal": 0},
        {"record_ordinal": -1, "stage_id": "execution"},
        {"record_ordinal": True, "stage_id": "execution"},
        {"record_ordinal": 2**63, "stage_id": "execution"},
        {"record_ordinal": "1 OR true", "stage_id": "execution"},
    ],
)
def test_ordinal_is_a_scoped_bounded_parameter_not_sql_or_page_index(tmp_path, query):
    trace, decision = observed_trace(tmp_path)
    for request in (decision_view, candidates_view):
        with pytest.raises(DomainError) as error:
            request(trace, decision, **query)
        assert (
            error.value.code == "invalid_trace_query" and error.value.status_code == 422
        )
    from packages.research_trace.storage import TraceStorageError

    with pytest.raises(TraceStorageError) as error:
        trace.reader.query(
            "features",
            decision_id=decision,
            start=date(2024, 2, 1),
            end=date(2024, 2, 2),
            **query,
        )
    assert error.value.code == "invalid_trace_query"


def test_event_table_cannot_accept_a_detail_record_ordinal(tmp_path):
    from packages.research_trace.storage import TraceStorageError

    trace, _ = observed_trace(tmp_path)
    with pytest.raises(TraceStorageError) as error:
        trace.reader.query("events", stage_id="execution", record_ordinal=0)
    assert error.value.code == "invalid_trace_query"
