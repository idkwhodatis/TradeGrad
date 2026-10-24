"""Real DuckDB/Parquet invariant tests with explicitly synthetic observations."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from packages.research_trace.comparison import (
    ComparisonControl,
    ComparisonReader,
    compare_evaluations,
    comparison_input_hash,
)
from packages.research_trace.comparison_contracts import (
    ComparisonError,
    ComparisonInput,
    ComparisonLimits,
    ComparisonPublication,
    EventNamespace,
)
from packages.research_trace.contracts import (
    CaptureConfig,
    DateRange,
    EvaluationBinding,
    digest,
)
from packages.research_trace.recorder import Recorder
from packages.research_trace.storage import write_trace_artifacts

SOURCE = "class Example:\n    pass\n"
MOMENT = "2024-01-02T16:00:00+00:00"


def table(rows, names=()):
    names = list(rows[0]) if rows else list(names)
    return {
        "columns": [{"name": name} for name in names],
        "rows": [[row[name] for name in names] for row in rows],
    }


def economics(*, suffix="a", equity=100.0, open_position=True, orders=True):
    order = {
        "id": f"order-{suffix}",
        "symbol": "B",
        "side": "Buy",
        "quantity": 2,
        "filled_quantity": 2,
        "created_at": MOMENT,
        "updated_at": MOMENT,
        "commission": 0.0,
        "status": "Filled",
        "reject_reason": "",
    }
    fill = {
        "id": f"fill-{suffix}",
        "order_id": order["id"],
        "symbol": "B",
        "side": "Buy",
        "quantity": 2,
        "price": 10.0,
        "commission": 0.0,
        "timestamp": MOMENT,
    }
    position = {
        "date": MOMENT,
        "symbol": "B",
        "long_shares": 2,
        "short_shares": 0,
        "equity": 20.0,
        "close": 10.0,
    }
    return {
        "evaluation_mode": "native_akquant",
        "orders": table([order] if orders else [], order),
        "fills": table([fill] if orders else [], fill),
        "positions": table([position] if open_position else [], position),
        "closed_trades": table([]),
        "equity": {"rows": [[MOMENT, equity]]},
        "cash": {"rows": [[MOMENT, 80.0]]},
    }


def candidate(name="B", *, selected=True, score=1.0):
    return {
        "instrument_id": name,
        "universe_member": True,
        "eligibility": "eligible",
        "reason": "selected" if selected else "rank_not_selected",
        "score": score,
        "score_validity": "valid",
        "rank": 1 if selected else 2,
        "ranking_direction": "descending",
        "tie_break": "instrument_id",
        "selected": selected,
        "prior_holding": False,
    }


def feature(value=1.0):
    return {
        "feature_id": "momentum",
        "instrument_id": "B",
        "value": value,
        "validity": "valid",
        "unit": "ratio",
        "calculation_window": "1 session",
        "information_cutoff": MOMENT,
    }


@pytest.fixture
def namespace():
    return EventNamespace(
        dataset_version_id=uuid4(),
        dataset_sha256="b" * 64,
        universe_version_id=None,
        universe_sha256=None,
        frozen_universe_sha256="f" * 64,
        instrument_namespace="native_symbol",
    )


def make_input(
    path,
    namespace,
    *,
    event_ids=("frozen:one",),
    candidates=None,
    features=None,
    targets="default",
    capture=None,
    partial=False,
    suffix="a",
    code=SOURCE,
    bundle=None,
    no_trace=False,
    seq=0,
    stage_id="ranking",
):
    config = CaptureConfig.model_validate(capture or {"mode": "research"})
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    binding = EvaluationBinding(
        run_id=uuid4(),
        execution_attempt_id=uuid4(),
        evaluation_id=uuid4(),
        candidate_id=None,
        strategy_version_id=uuid4(),
        code_sha256=code_hash,
        dataset_version_id=namespace.dataset_version_id,
        dataset_sha256=namespace.dataset_sha256,
        universe_version_id=namespace.universe_version_id,
        universe_sha256=namespace.universe_sha256,
        execution_config_sha256="c" * 64,
        engine_dependencies_sha256="d" * 64,
        result_manifest_sha256="e" * 64,
    )
    recorder = Recorder(code_sha256=code_hash, config=config)
    for index, event_id in enumerate(event_ids):
        recorder.begin_decision(
            data_event_id=event_id,
            event_seq=seq + index,
            scope="portfolio",
            callback_phase="native.execution_result"
            if event_id.startswith("native_order:")
            else "close",
            decision_time=MOMENT,
            information_cutoff=MOMENT,
            market_timezone="UTC",
            pre_state={
                "cash": 100.0,
                "equity": 100.0,
                "positions": [],
                "open_orders": [],
                "coverage": "complete",
                "source": "engine_observed",
            },
        )
        recorder.emit(
            stage_id,
            {
                "candidates": candidates if candidates is not None else [candidate()],
                "features": features or [],
                "raw_targets": [{"instrument_id": "B", "weight": 0.2}]
                if targets == "default"
                else targets,
                "constrained_plan": [{"instrument_id": "B", "weight": 0.2}],
                "coverage": "partial" if partial else "complete",
                "missing_reason": "Captured detail was incomplete" if partial else None,
                "order_links": [
                    {
                        "logical_order_id": f"intent-{suffix}",
                        "child_order_ids": [f"order-{suffix}"],
                        "fill_ids": [f"fill-{suffix}"],
                        "instrument_id": "B",
                        "submitted_at": MOMENT,
                        "fill_times": [MOMENT],
                        "status": "filled",
                    }
                ],
            },
        )
        recorder.end_decision()
    manifest, artifacts = (
        (None, [])
        if no_trace
        else write_trace_artifacts(recorder.records, binding, recorder.summary(), path)
    )
    return ComparisonInput(
        binding=binding,
        source_kind="strategy_version",
        source_id=binding.strategy_version_id,
        source=code,
        window=DateRange(start="2024-01-02", end="2024-01-04"),
        spec=None,
        spec_sha256=None,
        trace_manifest=manifest,
        trace_manifest_sha256=digest(manifest) if manifest else None,
        trace_paths={row.name: row.path for row in artifacts},
        common_config={
            "window": {"start": "2024-01-02", "end": "2024-01-04"},
            "fees": {"rate": 0},
        },
        economic_bundle=bundle if bundle is not None else economics(suffix=suffix),
        namespace=namespace,
    )


def run_compare(path, left, right, **kwargs):
    publication = ComparisonPublication(
        comparison_id=uuid4(), run_id=uuid4(), execution_attempt_id=uuid4()
    )
    control = kwargs.pop("control", ComparisonControl())
    manifest, artifacts = compare_evaluations(
        left, right, path, publication=publication, control=control, **kwargs
    )
    assert control.closure_confirmed and control.active_connections == 0
    return (
        manifest,
        ComparisonReader(manifest, {item.name: item.path for item in artifacts}),
        artifacts,
    )


def test_real_full_outer_source_pairing_and_exact_two_prestates(tmp_path, namespace):
    left = make_input(tmp_path / "left", namespace)
    right = make_input(tmp_path / "right", namespace, suffix="b")
    manifest, reader, artifacts = run_compare(tmp_path / "out", left, right)
    row = reader.page("events").items[0]
    assert row.left["decision_id"] != row.right["decision_id"]
    assert row.left["pre_state"]["cash"] == row.right["pre_state"]["cash"] == 100
    assert row.status == "equal"
    assert (
        manifest.sections["orders"].changed == manifest.sections["fills"].changed == 0
    )
    assert "candidates" in manifest.observed_equal_sections
    assert manifest.diagnostic_parity_certified is manifest.causal_claim is False
    assert artifacts[-1].media_type == "application/json"
    assert all(item.path.read_bytes()[:4] == b"PAR1" for item in artifacts[:-1])


@pytest.mark.parametrize(
    "event_id",
    [
        "native_order:random-order",
        "2024-01-02",
        "2024-01-02T16:00:00Z",
        "550e8400-e29b-41d4-a716-446655440000",
    ],
)
def test_generic_uuid_and_date_only_events_never_pair(tmp_path, namespace, event_id):
    left = make_input(tmp_path / "left", namespace, event_ids=(event_id,))
    right = make_input(tmp_path / "right", namespace, suffix="b", event_ids=(event_id,))
    _, reader, _ = run_compare(tmp_path / "out", left, right)
    rows = reader.page("events").items
    assert len(rows) == 2 and all(not (row.left and row.right) for row in rows)
    assert all("unpaired" in (row.left_state, row.right_state) for row in rows)


def test_same_date_distinct_callbacks_sequences_retained_and_unmatched(
    tmp_path, namespace
):
    left = make_input(
        tmp_path / "left", namespace, event_ids=("frozen:one", "frozen:two")
    )
    right = make_input(
        tmp_path / "right", namespace, suffix="b", event_ids=("frozen:one",)
    )
    _, reader, _ = run_compare(tmp_path / "out", left, right)
    rows = reader.page("events").items
    assert len(rows) == 2
    assert sum(row.left is not None and row.right is not None for row in rows) == 1
    assert (
        next(row for row in rows if row.right is None).right_state == "observed_absence"
    )


def test_changed_namespace_disables_event_pairing(tmp_path, namespace):
    right_namespace = namespace.model_copy(update={"frozen_universe_sha256": "a" * 64})
    left = make_input(tmp_path / "left", namespace)
    right = make_input(tmp_path / "right", right_namespace, suffix="b")
    manifest, reader, _ = run_compare(tmp_path / "out", left, right)
    assert manifest.namespace_status == "different"
    assert reader.page("events").total == 2
    assert all(
        row.left is None or row.right is None for row in reader.page("events").items
    )


def test_duplicate_candidates_preserve_multiplicity_and_ambiguity(tmp_path, namespace):
    left = make_input(
        tmp_path / "left", namespace, candidates=[candidate(), candidate()]
    )
    right = make_input(
        tmp_path / "right", namespace, candidates=[candidate()], suffix="b"
    )
    manifest, reader, _ = run_compare(tmp_path / "out", left, right)
    row = reader.page("candidates").items[0]
    assert row.status == "changed" and row.ambiguous
    assert [item["ordinal"] for item in row.left["items"]] == [0, 1]
    assert len(row.right["items"]) == 1
    assert manifest.observed_behavior == "changed"


def test_duplicate_features_are_multisets_not_first_row_matching(tmp_path, namespace):
    left = make_input(tmp_path / "left", namespace, features=[feature(1), feature(2)])
    right = make_input(
        tmp_path / "right", namespace, features=[feature(2), feature(1)], suffix="b"
    )
    _, reader, _ = run_compare(tmp_path / "out", left, right)
    row = reader.page("features").items[0]
    assert row.status == "equal" and row.ambiguous
    assert [item["record"]["value"] for item in row.left["items"]] == [1, 2]
    assert [item["record"]["value"] for item in row.right["items"]] == [2, 1]


@pytest.mark.parametrize(
    "capture",
    [
        {"mode": "research", "instrument_ids": ["B"]},
        {"mode": "research", "stage_ids": ["ranking"]},
        {
            "mode": "research",
            "observation_window": {"start": "2024-01-02", "end": "2024-01-03"},
        },
    ],
)
def test_identical_filtered_capture_never_claims_full_section_equality(
    tmp_path, namespace, capture
):
    left = make_input(tmp_path / "left", namespace, capture=capture)
    right = make_input(tmp_path / "right", namespace, capture=capture, suffix="b")
    manifest, _, _ = run_compare(tmp_path / "out", left, right)
    assert manifest.sections["candidates"].observed_equality == "unknown"
    assert manifest.sections["candidates"].left_availability == "partial"
    assert "candidates" not in manifest.observed_equal_sections


def test_identical_incomplete_observations_remain_unknown(tmp_path, namespace):
    left = make_input(tmp_path / "left", namespace, partial=True)
    right = make_input(tmp_path / "right", namespace, partial=True, suffix="b")
    manifest, _, _ = run_compare(tmp_path / "out", left, right)
    assert manifest.sections["candidates"].observed_equality == "unknown"


def test_target_omission_is_not_explicit_zero_liquidation(tmp_path, namespace):
    left = make_input(tmp_path / "left", namespace, targets=[])
    right = make_input(
        tmp_path / "right",
        namespace,
        targets=[{"instrument_id": "B", "weight": 0.0}],
        suffix="b",
    )
    manifest, reader, _ = run_compare(tmp_path / "out", left, right)
    rows = reader.page("targets").items
    assert any(row.status == "right_only" and row.instrument_id == "B" for row in rows)
    assert manifest.sections["targets"].observed_equality == "different"


def test_missing_targets_and_absent_features_not_empty_equal(tmp_path, namespace):
    left = make_input(tmp_path / "left", namespace, targets=None)
    right = make_input(tmp_path / "right", namespace, targets=None, suffix="b")
    manifest, _, _ = run_compare(tmp_path / "out", left, right)
    assert manifest.sections["features"].observed_equality == "unknown"
    assert "features" not in manifest.observed_equal_sections
    assert manifest.sections["targets"].observed_equality == "unknown"
    assert manifest.sections["targets"].left_availability == "partial"


def test_missing_trace_and_registered_off_are_distinct_pins(tmp_path, namespace):
    left = make_input(tmp_path / "left", namespace, no_trace=True)
    right = make_input(
        tmp_path / "right", namespace, capture={"mode": "off"}, suffix="b"
    )
    manifest, reader, _ = run_compare(tmp_path / "out", left, right)
    assert (
        manifest.left.trace_state == "historical_absence"
        and manifest.left.trace_manifest_sha256 is None
    )
    assert (
        manifest.right.trace_state == "registered_off"
        and manifest.right.trace_manifest_sha256 is not None
    )
    assert reader.page("events").items == []
    assert manifest.sections["candidates"].left_availability == "not_collected"


def test_config_field_types_and_absent_metadata_are_not_equal(tmp_path, namespace):
    left = make_input(tmp_path / "left", namespace)
    right = make_input(tmp_path / "right", namespace, suffix="b")
    left = replace(left, common_config={"fees": {"enabled": False, "amount": 0}})
    right = replace(right, common_config={"fees": {"enabled": 0, "amount": 0.0}})
    manifest, _, _ = run_compare(tmp_path / "out", left, right)
    diff = {row.field: row for row in manifest.configuration_differences}
    assert diff["fees.enabled"].status == diff["fees.amount"].status == "different"
    assert diff["RNG"].status == "unknown" and not diff["RNG"].left_present


def test_turnover_definition_and_censored_open_holding_duration(tmp_path, namespace):
    left = make_input(tmp_path / "left", namespace)
    right = make_input(tmp_path / "right", namespace, suffix="b")
    manifest, reader, _ = run_compare(tmp_path / "out", left, right)
    turnover = reader.page("turnover").items[0].left["items"][0]["record"]
    assert turnover["value"] == 0.2 and turnover["denominator"] == 100
    assert (
        turnover["title"] == "Gross observed-window turnover"
        and turnover["annualized"] is False
    )
    holding = reader.page("holding_duration").items[0].left["items"][0]["record"]
    assert (
        holding["right_censored"]
        and holding["duration_seconds"] is None
        and holding["entry_time"] is None
    )
    assert manifest.sections["holding_duration"].observed_equality == "unknown"


@pytest.mark.parametrize("equity", [0.0, -1.0, None])
def test_invalid_turnover_denominator_is_null_not_zero(tmp_path, namespace, equity):
    left = make_input(tmp_path / "left", namespace, bundle=economics(equity=equity))
    right = make_input(tmp_path / "right", namespace, suffix="b")
    manifest, reader, _ = run_compare(tmp_path / "out", left, right)
    row = reader.page("turnover").items[0]
    assert (
        row.status == "unavailable" and row.left["items"][0]["record"]["value"] is None
    )
    assert manifest.sections["turnover"].observed_equality == "unknown"


def test_exact_source_trace_and_namespace_hashes_fail_closed(tmp_path, namespace):
    left = make_input(tmp_path / "left", namespace)
    right = make_input(tmp_path / "right", namespace, suffix="b")
    for changed in (
        replace(left, source=SOURCE + "#changed"),
        replace(left, trace_manifest_sha256="0" * 64),
        replace(
            left, namespace=namespace.model_copy(update={"dataset_sha256": "0" * 64})
        ),
    ):
        with pytest.raises(ComparisonError):
            comparison_input_hash(changed, right)


def test_semantic_cache_key_excludes_publication_but_includes_spec_config_namespace(
    tmp_path, namespace
):
    left = make_input(tmp_path / "left", namespace)
    right = make_input(tmp_path / "right", namespace, suffix="b")
    first = comparison_input_hash(left, right)
    assert first == comparison_input_hash(left, right)
    assert first != comparison_input_hash(
        left, replace(right, common_config={"fees": {"rate": 0.1}})
    )
    a, _, _ = run_compare(tmp_path / "one", left, right)
    b, _, _ = run_compare(tmp_path / "two", left, right)
    assert a.input_hash == b.input_hash == first and a.publication != b.publication


@pytest.mark.parametrize("fault", ["tamper", "symlink", "missing"])
def test_admitted_input_inode_and_hash_verification(tmp_path, namespace, fault):
    left = make_input(tmp_path / "left", namespace)
    right = make_input(tmp_path / "right", namespace, suffix="b")
    path = next(path for name, path in left.trace_paths.items() if "candidates" in name)
    raw = path.read_bytes()
    path.unlink()
    if fault == "tamper":
        path.write_bytes(raw[:-1] + b"!")
    elif fault == "symlink":
        target = tmp_path / "other.parquet"
        target.write_bytes(raw)
        path.symlink_to(target)
    with pytest.raises(ComparisonError) as error:
        run_compare(tmp_path / "out", left, right)
    assert str(tmp_path) not in str(error.value)


def test_reader_tamper_bounded_filters_and_exact_event(tmp_path, namespace):
    left = make_input(tmp_path / "left", namespace)
    right = make_input(tmp_path / "right", namespace, suffix="b")
    _, reader, artifacts = run_compare(tmp_path / "out", left, right)
    event = reader.page("events").items[0]
    assert reader.page("candidates", event_pair_id=event.event_pair_id).total == 1
    assert reader.page("candidates", instrument_id="' OR true --").total == 0
    for args in ({"limit": 501}, {"offset": 100001}, {"offset": -1}):
        with pytest.raises(ComparisonError):
            reader.page("events", **args)
    target = next(row.path for row in artifacts if row.section == "candidates")
    raw = target.read_bytes()
    target.unlink()
    target.write_bytes(raw[:-1] + b"!")
    with pytest.raises(ComparisonError):
        reader.page("candidates")


def test_aggregate_input_and_output_limits_fail_instead_of_truncate(
    tmp_path, namespace
):
    left = make_input(tmp_path / "left", namespace)
    right = make_input(tmp_path / "right", namespace, suffix="b")
    for limits in (
        ComparisonLimits(max_input_rows=1),
        ComparisonLimits(max_output_bytes=1024),
        ComparisonLimits(max_files=1),
    ):
        with pytest.raises(ComparisonError) as error:
            run_compare(tmp_path / str(uuid4()), left, right, limits=limits)
        assert error.value.code == "comparison_capacity"


def test_normal_control_close_preserves_external_cancel_event():
    event = threading.Event()
    control = ComparisonControl(cancel_event=event)
    with control.connection() as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)
    control.close()
    assert not event.is_set() and control.closure_confirmed


def test_owned_real_duckdb_long_query_is_cancelled_and_synchronously_closed():
    event = threading.Event()
    control = ComparisonControl(cancel_event=event, timeout_seconds=30)
    started, errors = threading.Event(), []

    def work():
        try:
            with control.connection() as connection:
                started.set()
                connection.execute(
                    "SELECT sum(sin(i)) FROM range(10000000000) t(i)"
                ).fetchone()
        except ComparisonError as error:
            errors.append(error.code)

    worker = threading.Thread(target=work)
    worker.start()
    assert started.wait(2)
    event.set()
    worker.join(5)
    assert not worker.is_alive() and errors == ["comparison_cancelled"]
    assert control.closure_confirmed and control.active_connections == 0


def test_permanent_close_failure_retains_registry_until_process_exit(tmp_path):
    script = r"""
import json, threading, time
from packages.research_trace import comparison_storage as storage
class Broken:
    def interrupt(self): pass
    def close(self): raise RuntimeError('unconfirmed')
storage._connect = Broken
control = storage.ComparisonControl()
def run():
    with control.connection(): pass
worker = threading.Thread(target=run, daemon=True)
worker.start()
time.sleep(.7)
print(json.dumps({'active':control.active_connections,'unconfirmed':control.closure_unconfirmed,'confirmed':control.closure_confirmed,'alive':worker.is_alive()}),flush=True)
while True: time.sleep(1)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[2],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        import select

        assert select.select([process.stdout], [], [], 4)[0]
        state = json.loads(process.stdout.readline())
        assert state == {
            "active": 1,
            "unconfirmed": True,
            "confirmed": False,
            "alive": True,
        }
        assert process.poll() is None
    finally:
        process.kill()
        process.wait(timeout=3)


def test_pre_cancel_and_deadline_never_publish(tmp_path, namespace):
    left = make_input(tmp_path / "left", namespace)
    right = make_input(tmp_path / "right", namespace, suffix="b")
    event = threading.Event()
    event.set()
    for control, code in (
        (ComparisonControl(cancel_event=event), "comparison_cancelled"),
        (ComparisonControl(deadline=time.monotonic() - 1), "comparison_timeout"),
    ):
        with pytest.raises(ComparisonError) as error:
            run_compare(tmp_path / "out", left, right, control=control)
        assert error.value.code == code
        assert control.closure_confirmed
    assert not (tmp_path / "out").exists()


def test_complete_observed_behavior_unchanged_despite_code_comment(tmp_path, namespace):
    left = make_input(
        tmp_path / "left", namespace, bundle=economics(open_position=False)
    )
    right = make_input(
        tmp_path / "right",
        namespace,
        suffix="b",
        code=SOURCE + "# comment\n",
        bundle=economics(suffix="b", open_position=False),
    )
    manifest, _, _ = run_compare(tmp_path / "out", left, right)
    assert manifest.code_changed and manifest.observed_behavior == "unchanged_observed"
    assert {
        "events",
        "candidates",
        "targets",
        "orders",
        "fills",
        "account",
        "turnover",
    } <= set(manifest.observed_equal_sections)
    assert manifest.diagnostic_parity_certified is False


@pytest.mark.parametrize(
    "template_id,parameter,value,changed",
    [
        ("daily_timing", "liquidity_enabled", False, {"liquidity"}),
        ("daily_timing", "max_weight", 0.15, {"allocation"}),
        ("daily_timing", "exit_buffer", 0.07, {"signal"}),
        ("cross_sectional", "liquidity_enabled", False, {"liquidity", "ranking"}),
        ("cross_sectional", "max_weight", 0.15, {"allocation"}),
        ("cross_sectional", "top_n", 2, {"ranking", "allocation"}),
    ],
)
def test_controlled_parameter_diff_has_reviewed_stage_granularity(
    tmp_path, namespace, template_id, parameter, value, changed
):
    from packages.research_trace.comparison import _mechanisms
    from packages.research_trace.templates import template_source, template_spec

    source = template_source(template_id)
    left = make_input(tmp_path / "left", namespace, code=source)
    right = make_input(tmp_path / "right", namespace, code=source, suffix="b")
    lspec, rspec = (
        template_spec(template_id),
        template_spec(template_id, parameters={parameter: value}),
    )
    left, right = (
        replace(left, spec=lspec, spec_sha256=digest(lspec)),
        replace(right, spec=rspec, spec_sha256=digest(rspec)),
    )
    rows = _mechanisms(left, right)
    assert {row.rule_id for row in rows if row.status == "changed"} == changed
    assert all(row.mapping == "controlled" for row in rows)
    assert all(row.status == "equal" for row in rows if row.rule_id not in changed)


def test_economic_observation_window_does_not_inherit_capture_filter(
    tmp_path, namespace
):
    capture = {
        "mode": "research",
        "observation_window": {"start": "2024-01-02", "end": "2024-01-03"},
    }
    left = make_input(tmp_path / "left", namespace, capture=capture)
    right = make_input(tmp_path / "right", namespace, suffix="b", capture=capture)
    manifest, _, _ = run_compare(tmp_path / "out", left, right)
    assert (
        manifest.sections["candidates"].left_scope["observation_window"]["end"]
        == "2024-01-03"
    )
    assert (
        manifest.sections["positions"].left_scope["observation_window"]["end"]
        == "2024-01-04"
    )


def test_lost_detail_counters_prevent_equal_retained_section(tmp_path, namespace):
    left = make_input(tmp_path / "left", namespace)
    right = make_input(tmp_path / "right", namespace, suffix="b")

    def lost(value):
        manifest = value.trace_manifest.model_copy(
            update={
                "coverage": "partial",
                "missing_reasons": ["A later detail stage exceeded capture budget"],
                "detail_rows_seen": value.trace_manifest.detail_rows_seen + 1000,
            }
        )
        return replace(
            value, trace_manifest=manifest, trace_manifest_sha256=digest(manifest)
        )

    manifest, _, _ = run_compare(tmp_path / "out", lost(left), lost(right))
    assert manifest.sections["candidates"].observed_equality == "unknown"
    assert manifest.observed_behavior == "insufficient_coverage"


def test_actual_closed_trade_duration_uses_saved_entry_exit_not_fill_date(
    tmp_path, namespace
):
    bundle = economics(open_position=False)
    trade = {
        "symbol": "B",
        "entry_time": "2024-01-01T16:00:00+00:00",
        "exit_time": MOMENT,
        "quantity": 1,
        "pnl": 2.0,
        "net_pnl": 2.0,
        "commission": 0.0,
    }
    bundle["closed_trades"] = table([trade])
    left = make_input(tmp_path / "left", namespace, bundle=bundle)
    other = economics(suffix="b", open_position=False)
    other["closed_trades"] = table([trade])
    right = make_input(tmp_path / "right", namespace, suffix="b", bundle=other)
    manifest, reader, _ = run_compare(tmp_path / "out", left, right)
    row = reader.page("holding_duration").items[0].left["items"][0]["record"]
    assert row["duration_seconds"] == 86400 and row["right_censored"] is False
    assert manifest.sections["holding_duration"].observed_equality == "equal"


def test_transient_close_failure_is_not_proof_until_success(monkeypatch):
    from packages.research_trace import comparison_storage

    external = threading.Event()
    calls = []

    class Connection:
        def interrupt(self):
            calls.append("interrupt")

        def close(self):
            calls.append("close")
            if calls.count("close") == 1:
                raise RuntimeError("unconfirmed closure")

    monkeypatch.setattr(comparison_storage, "_connect", Connection)
    control = ComparisonControl(cancel_event=external)
    with control.connection():
        pass
    assert calls.count("close") == 2 and "interrupt" in calls
    assert control.closure_confirmed and not external.is_set()


def test_watchdog_start_failure_still_closes_owned_connection(monkeypatch):
    from packages.research_trace import comparison_storage

    closed = []

    class Connection:
        def interrupt(self):
            pass

        def close(self):
            closed.append(True)

    class Watcher:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread budget exhausted")

    monkeypatch.setattr(comparison_storage, "_connect", Connection)
    monkeypatch.setattr(comparison_storage.threading, "Thread", Watcher)
    control = ComparisonControl()
    with pytest.raises(RuntimeError), control.connection():
        pass
    assert closed == [True] and control.closure_confirmed


def test_real_connection_is_fixed_memory_single_thread_no_spill_or_extensions():
    control = ComparisonControl()
    with control.connection() as connection:
        values = connection.execute(
            "SELECT current_setting('threads'), current_setting('memory_limit'), current_setting('max_temp_directory_size'), current_setting('autoinstall_known_extensions'), current_setting('autoload_known_extensions')"
        ).fetchone()
    assert values == (1, "256.0 MiB", "0 bytes", False, False)


def test_timeout_interrupts_long_query_and_confirms_closure():
    control = ComparisonControl(timeout_seconds=0.05)
    with pytest.raises(ComparisonError) as error, control.connection() as connection:
        connection.execute("SELECT sum(sin(i)) FROM range(10000000000) t(i)").fetchone()
    assert error.value.code == "comparison_timeout" and control.closure_confirmed


def test_association_targets_are_explicit_typed_replay_links(tmp_path, namespace):
    left = make_input(tmp_path / "left", namespace)
    right = make_input(tmp_path / "right", namespace, suffix="b")
    _, reader, _ = run_compare(tmp_path / "out", left, right)
    event = reader.page("events").items[0]
    order = reader.page("orders").items[0]
    assert order.event_pair_id == event.event_pair_id
    assert {(item.side, item.decision_id) for item in order.associations} == {
        ("left", event.left["decision_id"]),
        ("right", event.right["decision_id"]),
    }
    assert all(
        item.child_order_ids and item.source_event_paired for item in order.associations
    )


def test_numeric_tolerance_multiset_matching_is_not_greedy_or_order_dependent():
    from packages.research_trace.comparison_semantics import multiset_equal

    left = [{"score": 0.0}, {"score": 1e-12}]
    right = [{"score": 1e-12}, {"score": 2e-12}]
    assert multiset_equal(left, right) is True
    assert multiset_equal(left[::-1], right) is True
    assert multiset_equal(left, right[::-1]) is True


def graph_input(path, namespace, *, suffix, split=False, missing=False):
    base = make_input(path / "initial", namespace, suffix=suffix)
    first = economics(suffix=suffix)
    other = economics(suffix=suffix + "2")
    names = [column["name"] for column in first["orders"]["columns"]]
    order_rows = [
        dict(zip(names, first["orders"]["rows"][0], strict=True)),
        dict(zip(names, other["orders"]["rows"][0], strict=True)),
    ]
    names = [column["name"] for column in first["fills"]["columns"]]
    fill_rows = [
        dict(zip(names, first["fills"]["rows"][0], strict=True)),
        dict(zip(names, other["fills"]["rows"][0], strict=True)),
    ]
    first["orders"], first["fills"] = table(order_rows), table(fill_rows)
    recorder = Recorder(
        code_sha256=base.binding.code_sha256, config=CaptureConfig(mode="research")
    )
    recorder.begin_decision(
        data_event_id="frozen:graph",
        event_seq=0,
        scope="portfolio",
        callback_phase="close",
        decision_time=MOMENT,
        information_cutoff=MOMENT,
        market_timezone="UTC",
        pre_state={
            "cash": 100.0,
            "equity": 100.0,
            "positions": [],
            "open_orders": [],
            "coverage": "complete",
            "source": "engine_observed",
        },
    )
    groups = [[0], [1]] if split else [[0, 1]]
    links = [
        {
            "logical_order_id": f"logical-{suffix}-{index}",
            "child_order_ids": [order_rows[item]["id"] for item in group],
            "fill_ids": [fill_rows[item]["id"] for item in group],
            "instrument_id": "B",
            "submitted_at": MOMENT,
            "fill_times": [MOMENT for _ in group],
            "status": "filled",
        }
        for index, group in enumerate(groups)
    ]
    recorder.emit(
        "execution",
        {
            "order_links": [] if missing else links,
            "raw_targets": [],
            "constrained_plan": [],
        },
    )
    recorder.end_decision()
    manifest, artifacts = write_trace_artifacts(
        recorder.records, base.binding, recorder.summary(), path / "graph"
    )
    return replace(
        base,
        trace_manifest=manifest,
        trace_manifest_sha256=digest(manifest),
        trace_paths={item.name: item.path for item in artifacts},
        economic_bundle=first,
    )


def test_order_graph_topology_is_not_erased_with_random_local_ids(tmp_path, namespace):
    left = graph_input(tmp_path / "left", namespace, suffix="a", split=False)
    right = graph_input(tmp_path / "right", namespace, suffix="b", split=True)
    manifest, reader, _ = run_compare(tmp_path / "out", left, right)
    row = reader.page("orders").items[0]
    assert row.status == "changed" and row.ambiguous
    assert len(row.left["items"]) == len(row.right["items"]) == 2
    assert {item["record"]["id"] for item in row.left["items"]} == {
        "order-a",
        "order-a2",
    }
    assert manifest.sections["orders"].observed_equality == "different"


def test_unknown_order_graph_does_not_turn_identical_economic_rows_into_changed(
    tmp_path, namespace
):
    left = graph_input(tmp_path / "left", namespace, suffix="a", missing=True)
    right = graph_input(tmp_path / "right", namespace, suffix="b")
    manifest, reader, _ = run_compare(tmp_path / "out", left, right)
    row = reader.page("orders").items[0]
    assert row.status == "equal" and row.left_state == "incomplete_coverage"
    assert manifest.sections["orders"].observed_equality == "unknown"
    assert manifest.sections["orders"].changed == 0


def test_one_logical_order_multiple_fills_preserves_actual_fill_multiplicity(
    tmp_path, namespace
):
    left = graph_input(tmp_path / "left", namespace, suffix="a")
    right = graph_input(tmp_path / "right", namespace, suffix="b")
    _, reader, _ = run_compare(tmp_path / "out", left, right)
    row = reader.page("fills").items[0]
    assert row.status == "equal" and row.ambiguous
    assert len(row.left["items"]) == len(row.right["items"]) == 2
    assert all(item["record"]["order_id"] for item in row.left["items"])


def test_nested_economic_graph_multisets_use_tolerance_without_sort_pairing(
    tmp_path, namespace
):
    left = graph_input(tmp_path / "left", namespace, suffix="a")
    right = graph_input(tmp_path / "right", namespace, suffix="b")
    for value, fees in ((left, [0.0, 1e-8]), (right, [1e-8, 0.0])):
        for section in ("orders", "fills"):
            data = value.economic_bundle[section]
            names = [column["name"] for column in data["columns"]]
            for index, row in enumerate(data["rows"]):
                row[names.index("quantity")] = index + 1
                row[names.index("commission")] = fees[index]
                if "filled_quantity" in names:
                    row[names.index("filled_quantity")] = index + 1
    manifest, reader, _ = run_compare(tmp_path / "out", left, right)
    for section in ("orders", "fills"):
        row = reader.page(section).items[0]
        assert row.status == "equal" and row.ambiguous
        assert manifest.sections[section].observed_equality == "equal"


def test_unordered_graph_paths_are_context_specific_and_keep_source_tuple_order():
    from packages.research_trace.comparison_semantics import equal_value

    left = {
        "order_graph": [
            {"children": [{"score": 1.0}, {"score": 2.0}], "source_event": ["a", "b"]}
        ]
    }
    right = {
        "order_graph": [
            {"children": [{"score": 2.0}, {"score": 1.0}], "source_event": ["a", "b"]}
        ]
    }
    assert equal_value(left, right, graph_semantics=True) is True
    assert equal_value(left, right) is False  # Arbitrary targets stay positional.
    right["order_graph"][0]["source_event"].reverse()
    assert equal_value(left, right, graph_semantics=True) is False


def test_nested_matching_unknown_and_work_budget_do_not_become_changed():
    from packages.research_trace.comparison_semantics import (
        _MatchingWork,
        equal_value,
        group_record,
    )

    left = {"order_graph": [{"children": [{"score": 0.0}] * 65}]}
    right = {"order_graph": [{"children": [{"score": 1e-13}] * 65}]}
    assert equal_value(left, right, graph_semantics=True) is None
    record = group_record(
        "orders",
        "key",
        [{"ordinal": 0, "semantic": left}],
        [{"ordinal": 0, "semantic": right}],
        left_state="observed",
        right_state="observed",
        graph_semantics=True,
    )
    assert record["status"] == "ambiguous" and record["ambiguous"]
    work = _MatchingWork(None, graph_semantics=True)
    work.nodes = 2
    assert equal_value(left, right, _work=work) is None
    work = _MatchingWork(None, graph_semantics=True)
    work.bytes = 1
    assert equal_value(left, right, _work=work) is None
    control = ComparisonControl()
    control.cancel()
    with pytest.raises(ComparisonError) as error:
        equal_value(left, right, control=control, graph_semantics=True)
    assert error.value.code == "comparison_cancelled"


def test_event_index_long_stage_ids_are_rejected_before_materialization(
    tmp_path, namespace
):
    from contextlib import ExitStack

    from packages.research_trace.comparison import _event_index
    from packages.research_trace.comparison_storage import TrustedTraceScan

    value = make_input(
        tmp_path / "input",
        namespace,
        event_ids=tuple(f"frozen:{i}" for i in range(8)),
        stage_id="long-stage-" + "x" * 180,
    )
    limits = ComparisonLimits(max_event_index_bytes=4096)
    control = ComparisonControl()
    queries = []
    with ExitStack() as stack, control.connection() as connection:
        scan = TrustedTraceScan(value, stack, control, limits)

        class PreflightOnly:
            def execute(self, query, args):
                queries.append(query)
                assert query.startswith("SELECT count(*)")
                return connection.execute(query, args)

        with pytest.raises(ComparisonError) as error:
            _event_index(PreflightOnly(), scan, control, limits)
    assert error.value.code == "comparison_capacity"
    assert len(queries) == 1 and 'encode("stage_ids")' in queries[0]
    assert control.closure_confirmed


@pytest.mark.parametrize("first,second", [(False, 0), (True, 1), (0, 0.0)])
def test_human_confirmed_mechanism_parameter_types_are_not_collapsed(
    tmp_path, namespace, first, second
):
    from packages.research_trace.comparison import _mechanisms
    from packages.research_trace.templates import template_source, template_spec

    source = template_source("daily_timing")
    base = make_input(tmp_path / "input", namespace, code=source)
    spec = template_spec("daily_timing")
    values = []
    for value in (first, second):
        rule = spec.rules[0].model_copy(
            update={"mapping": "human_confirmed", "parameters": {"flag": value}}
        )
        revised = spec.model_copy(
            update={"template_id": None, "edges": [], "rules": [rule]}
        )
        values.append(replace(base, spec=revised, spec_sha256=digest(revised)))
    rows = _mechanisms(*values)
    assert len(rows) == 1 and rows[0].status == "changed"


def test_multi_event_explicit_associations_are_indexed_without_arbitrary_primary(
    tmp_path, namespace
):
    ids = ("frozen:first", "frozen:second")
    left = make_input(tmp_path / "left", namespace, event_ids=ids)
    right = make_input(tmp_path / "right", namespace, event_ids=ids, suffix="b")
    manifest, reader, _ = run_compare(tmp_path / "out", left, right)
    order = reader.page("orders").items[0]
    assert order.event_pair_id is None
    paired = {row.event_pair_id for row in reader.page("events").items}
    assert {row.event_pair_id for row in order.associations} == paired
    assert {
        pair
        for ref in manifest.artifacts
        if ref.section == "orders"
        for pair in ref.event_pair_ids
    } == paired
    for pair in paired:
        page = reader.page("orders", event_pair_id=pair)
        assert page.total == 1 and page.items[0].row_id == order.row_id


def test_shutdown_during_failed_close_backoff_cannot_escape_guard(monkeypatch):
    from packages.research_trace import comparison_storage

    calls = []

    class Connection:
        def interrupt(self):
            pass

        def close(self):
            calls.append("close")
            if calls.count("close") == 1:
                raise RuntimeError("closure unknown")

    def interrupted_sleep(seconds):
        calls.append("backoff")
        raise KeyboardInterrupt

    monkeypatch.setattr(comparison_storage, "_connect", Connection)
    monkeypatch.setattr(comparison_storage.time, "sleep", interrupted_sleep)
    external = threading.Event()
    control = ComparisonControl(cancel_event=external)
    with control.connection():
        pass
    assert calls == ["close", "backoff", "close"]
    assert control.closure_confirmed and not external.is_set()
