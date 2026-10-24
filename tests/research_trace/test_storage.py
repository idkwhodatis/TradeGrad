"""Real DuckDB/Parquet tests using explicitly synthetic contract observations."""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from packages.research_trace.contracts import CaptureConfig, EvaluationBinding
from packages.research_trace.recorder import Recorder
from packages.research_trace.storage import (
    TraceReader,
    TraceStorageError,
    _connection,
    manifest_name,
    write_trace_artifacts,
)


@pytest.fixture
def binding():
    return EvaluationBinding(
        run_id=uuid4(),
        execution_attempt_id=uuid4(),
        evaluation_id=uuid4(),
        candidate_id=None,
        strategy_version_id=uuid4(),
        code_sha256="a" * 64,
        dataset_version_id=uuid4(),
        dataset_sha256="b" * 64,
        universe_version_id=None,
        universe_sha256=None,
        execution_config_sha256="c" * 64,
        engine_dependencies_sha256="d" * 64,
        result_manifest_sha256="e" * 64,
    )


def captured(*, days=2, code_hash="a" * 64, begin=date(2024, 1, 30)):
    recorder = Recorder(code_sha256=code_hash, config=CaptureConfig(mode="research"))
    for index in range(days):
        moment = datetime.combine(
            begin + timedelta(days=index // 2), datetime.min.time(), tzinfo=UTC
        )
        recorder.begin_decision(
            data_event_id=f"frozen:{index // 2}",
            event_seq=index % 2,
            scope="portfolio",
            callback_phase="close",
            information_cutoff=moment,
            decision_time=moment,
            market_timezone="UTC",
        )
        recorder.emit(
            "selection",
            {
                "candidates": [
                    {
                        "instrument_id": name,
                        "universe_member": True,
                        "eligibility": "eligible" if name == "B" else "ineligible",
                        "reason": "selected" if name == "B" else "eligibility_filter",
                        "score": 0.2 if name == "B" else 0.9,
                        "score_validity": "valid",
                        "rank": 1 if name == "B" else None,
                        "ranking_direction": "descending",
                        "tie_break": "instrument_id",
                        "selected": name == "B",
                        "prior_holding": False,
                    }
                    for name in ("B", "C")
                ],
                "features": [
                    {
                        "feature_id": "momentum",
                        "instrument_id": "B",
                        "value": 0.2,
                        "validity": "valid",
                        "unit": "fraction",
                        "calculation_window": "3 sessions",
                        "information_cutoff": moment,
                    }
                ],
                "predicates": [
                    {
                        "condition_id": f"liquid:{state}",
                        "instrument_id": "B",
                        "operator": "gte",
                        "operands": [1200] if state in {"true", "false"} else None,
                        "threshold": 1000,
                        "state": state,
                        "missing_reason": None
                        if state in {"true", "false"}
                        else f"Explicit {state}",
                    }
                    for state in (
                        "true",
                        "false",
                        "not_evaluated",
                        "unknown",
                        "not_sampled",
                    )
                ],
                "raw_targets": [{"instrument_id": "B", "weight": 1.0}],
                "constrained_plan": [{"instrument_id": "B", "weight": 0.5}],
            },
        )
        recorder.end_decision()
    return recorder


def store(tmp_path, binding, recorder):
    manifest, artifacts = write_trace_artifacts(
        iter(recorder.records), binding, recorder.summary(), tmp_path
    )
    reader = TraceReader(manifest, {item.name: item.path for item in artifacts})
    return manifest, artifacts, reader


def test_real_parquet_partitioned_stream_and_exact_same_timestamp_events(
    tmp_path, binding
):
    recorder = captured(days=10)
    manifest, artifacts, reader = store(tmp_path, binding, recorder)
    assert manifest.decisions_saved == 10
    assert manifest.detail_rows_saved == 80
    # One bounded date-range block per populated table, never per event/bar.
    assert len(artifacts) == 7
    assert all(
        ref.date_range.start == date(2024, 1, 30)
        and ref.date_range.end == date(2024, 2, 4)
        for ref in manifest.artifacts
    )
    assert artifacts[-1].name == manifest_name(binding.evaluation_id)
    assert all(item.path.read_bytes()[:4] == b"PAR1" for item in artifacts[:-1])
    page = reader.query(
        "events", start=date(2024, 1, 30), end=date(2024, 1, 31), limit=1
    )
    assert page["total"] == 2
    assert page["items"][0]["event_seq"] == 0
    assert reader.query("events", stage_id="selection")["total"] == 10
    assert reader.query("events", offset=99)["total"] == 10
    assert reader.query("events", offset=99)["items"] == []
    assert len({row["decision_id"] for row in reader.query("events")["items"]}) == 10


def test_candidates_feature_predicates_are_separate_bounded_projections(
    tmp_path, binding
):
    recorder = captured()
    _, _, reader = store(tmp_path, binding, recorder)
    query = {
        "decision_id": recorder.records[0].decision_id,
        "start": date(2024, 1, 30),
        "end": date(2024, 1, 31),
    }
    page = reader.query(
        "candidates", **query, eligibility="eligible", rank_min=1, rank_max=1
    )
    assert page["total"] == 1
    assert page["items"][0]["payload"]["instrument_id"] == "B"
    assert (
        reader.query("candidates", **query, reason="eligibility_filter")["items"][0][
            "payload"
        ]["instrument_id"]
        == "C"
    )
    assert reader.query("candidates", **query, limit=1)["total"] == 2
    assert (
        reader.query("candidates", **query, instrument_id="' OR true --")["total"] == 0
    )
    predicates = reader.query("predicates", **query)
    assert [item["payload"]["state"] for item in predicates["items"]] == [
        "true",
        "false",
        "not_evaluated",
        "unknown",
        "not_sampled",
    ]
    header = reader.query("decisions", **query)["items"][0]["payload"]
    assert header["pre_state"] is None
    assert header["pre_state_missing_reason"]
    assert "stages" not in header
    stage = reader.query("stages", **query)["items"][0]["payload"]
    assert stage["candidates_saved"] == 2
    assert "candidates" not in stage
    assert "features" not in stage


def test_exact_retry_and_conflicting_bytes_never_clobber(tmp_path, binding):
    recorder = captured()
    first, artifacts, _ = store(tmp_path, binding, recorder)
    second, _, _ = store(tmp_path, binding, recorder)
    assert first == second
    before = artifacts[-1].path.read_bytes()
    other = captured()
    with pytest.raises(TraceStorageError):
        store(tmp_path, binding, other)
    assert artifacts[-1].path.read_bytes() == before


@pytest.mark.parametrize("fault", ["tamper", "missing", "symlink"])
def test_selected_artifacts_fail_closed_without_path_leaks(tmp_path, binding, fault):
    recorder = captured()
    _, artifacts, reader = store(tmp_path, binding, recorder)
    item = next(item for item in artifacts if "-trace-candidates-" in item.name)
    original = item.path.read_bytes()
    item.path.unlink()
    if fault == "tamper":
        item.path.write_bytes(original[:-1] + b"!")
    elif fault == "symlink":
        other = tmp_path / "foreign.parquet"
        other.write_bytes(original)
        item.path.symlink_to(other)
    with pytest.raises(TraceStorageError) as error:
        reader.query(
            "candidates",
            decision_id=recorder.records[0].decision_id,
            start=date(2024, 1, 30),
            end=date(2024, 1, 31),
        )
    assert str(tmp_path) not in str(error.value)


def test_unselected_future_artifact_is_not_opened(tmp_path, binding, monkeypatch):
    from packages.research_trace import storage

    monkeypatch.setattr(storage, "BLOCK_ROWS", 4)
    recorder = captured(days=10)
    _, artifacts, reader = store(tmp_path, binding, recorder)
    next(
        item for item in artifacts if "-candidates-2024-02-" in item.name
    ).path.unlink()
    assert (
        reader.query(
            "candidates",
            decision_id=recorder.records[0].decision_id,
            start=date(2024, 1, 30),
            end=date(2024, 1, 31),
        )["total"]
        == 2
    )


@pytest.mark.parametrize(
    "query",
    [
        {"limit": 501},
        {"offset": 100001},
        {"limit": True},
        {"start": date(2024, 2, 1), "end": date(2024, 1, 1)},
    ],
)
def test_query_bounds_are_enforced_below_http(tmp_path, binding, query):
    _, _, reader = store(tmp_path, binding, captured())
    with pytest.raises(TraceStorageError, match="Invalid|precede"):
        reader.query("events", **query)


def test_bounded_scan_requires_narrower_selection(tmp_path, binding):
    manifest, artifacts, _ = store(tmp_path, binding, captured())
    reader = TraceReader(
        manifest, {item.name: item.path for item in artifacts}, max_scan_bytes=1
    )
    with pytest.raises(TraceStorageError) as error:
        reader.query("events")
    assert error.value.code == "trace_query_too_large"


def test_truncation_counters_are_not_normalized_and_invalid_summary_rejected(
    tmp_path, binding
):
    recorder = captured()
    summary = recorder.summary()
    summary.update(
        coverage="partial",
        missing_reasons=["Capacity limit reached"],
        decisions_seen=99,
        detail_rows_seen=999,
    )
    manifest, _ = write_trace_artifacts(recorder.records, binding, summary, tmp_path)
    assert (manifest.decisions_seen, manifest.decisions_saved) == (99, 2)
    assert (manifest.detail_rows_seen, manifest.detail_rows_saved) == (999, 16)
    summary["decisions_saved"] = 1
    with pytest.raises(TraceStorageError) as error:
        write_trace_artifacts(recorder.records, binding, summary, tmp_path / "mismatch")
    assert error.value.code == "trace_count_mismatch"
    assert not (tmp_path / "mismatch" / manifest_name(binding.evaluation_id)).exists()


def test_duplicate_event_or_foreign_source_anchor_rejected(tmp_path, binding):
    recorder = captured()
    summary = recorder.summary()
    summary.update(decisions_saved=2, detail_rows_saved=16)
    with pytest.raises(TraceStorageError):
        write_trace_artifacts([recorder.records[0]] * 2, binding, summary, tmp_path)
    raw = recorder.records[0].model_dump(mode="json")
    raw["stages"][0]["features"][0]["source_ref"] = {
        "code_sha256": "f" * 64,
        "file": "strategy.py",
        "symbol": "rule",
        "line_start": 1,
        "line_end": 1,
    }
    summary.update(decisions_saved=1, detail_rows_saved=8)
    with pytest.raises(TraceStorageError):
        write_trace_artifacts([raw], binding, summary, tmp_path)


def test_disabled_capture_returns_metadata_not_fabricated_rows(tmp_path, binding):
    recorder = Recorder(
        code_sha256=binding.code_sha256, config=CaptureConfig(mode="off")
    )
    manifest, artifacts, reader = store(tmp_path, binding, recorder)
    assert manifest.coverage == "not_collected"
    assert len(artifacts) == 1
    assert manifest.decisions_saved == 0
    assert reader.query("events")["total"] == 0


def test_duckdb_never_autoloads_extensions():
    with _connection() as connection:
        assert connection.execute(
            "SELECT current_setting('autoinstall_known_extensions'), current_setting('autoload_known_extensions')"
        ).fetchone() == (False, False)


def test_scores_are_finite_bounded_filters_not_sql(tmp_path, binding):
    recorder = captured()
    _, _, reader = store(tmp_path, binding, recorder)
    query = {
        "decision_id": recorder.records[0].decision_id,
        "start": date(2024, 1, 30),
        "end": date(2024, 1, 31),
    }
    page = reader.query("candidates", **query, score_min=0.5, score_max=1.0)
    assert page["total"] == 1
    assert page["items"][0]["payload"]["instrument_id"] == "C"
    for filters in (
        {"score_min": float("nan")},
        {"score_max": float("inf")},
        {"score_min": 1, "score_max": 0},
        {"score_max": "0 OR 1=1"},
    ):
        with pytest.raises(TraceStorageError) as error:
            reader.query("candidates", **query, **filters)
        assert error.value.code == "invalid_trace_query"


def test_real_http_projection_over_parquet_with_explicit_session_stub(
    tmp_path, binding, monkeypatch
):
    """Router/SQL integration only, NOT PostgreSQL ownership acceptance."""
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from backend.app import trace_routes
    from backend.app.api.deps import get_current_user, get_session
    from backend.app.services import DomainError
    from backend.app.trace_schemas import TraceContext
    from backend.app.trace_services import OwnedTrace

    recorder = captured()
    manifest, artifacts, reader = store(tmp_path, binding, recorder)
    trace = OwnedTrace(
        object(),
        TraceContext(
            evaluation_id=binding.evaluation_id,
            execution_attempt_id=binding.execution_attempt_id,
            result_manifest_sha256=binding.result_manifest_sha256,
            trace_manifest_sha256=artifacts[-1].sha256,
            status="available",
            binding=binding,
        ),
        manifest,
        reader,
    )
    actions = []
    session = SimpleNamespace(commit=lambda: actions.append("commit"))
    app = FastAPI()
    app.state.settings = SimpleNamespace()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid4())
    app.include_router(trace_routes.router)
    monkeypatch.setattr(trace_routes, "load_owned_trace", lambda *args, **kwargs: trace)
    monkeypatch.setattr(
        trace_routes,
        "record_verified_result_access",
        lambda *args: actions.append("exposure"),
    )

    @app.exception_handler(DomainError)
    async def error_handler(request, error):
        return JSONResponse(
            {"code": error.code, "detail": error.message}, status_code=error.status_code
        )

    base = f"/evaluations/{binding.evaluation_id}"
    decision = recorder.records[0].decision_id
    with TestClient(app) as client:
        for suffix in (
            "/trace-manifest",
            "/decisions",
            f"/decisions/{decision}",
            f"/decisions/{decision}/candidates",
        ):
            response = client.get(base + suffix)
            assert response.status_code == 200, response.text
            assert (
                response.json()["context"]["trace_manifest_sha256"]
                == artifacts[-1].sha256
            )
            assert actions[-2:] == ["exposure", "commit"]
        page = client.get(
            base + f"/decisions/{decision}/candidates", params={"score_min": 0.5}
        )
        assert page.json()["total"] == 1
        assert page.json()["items"][0]["observation"]["instrument_id"] == "C"
        assert (
            client.get(
                base + f"/decisions/{decision}/candidates", params={"score_min": "nan"}
            ).status_code
            == 422
        )
        assert client.get(base + "/decisions/" + "a" * 201).status_code == 422
        assert (
            client.get(
                base + f"/decisions/{decision}",
                params={"instrument_id": "B", "limit": 2},
            ).json()["predicates"]["total"]
            == 5
        )
        assert (
            client.get(
                base + "/decisions", params={"start": "2025-01-01", "end": "2024-01-01"}
            ).status_code
            == 422
        )


@pytest.mark.parametrize(
    "schema",
    [
        "tradegrad.run.v1",
        "tradegrad.final-evaluation.v1",
        "tradegrad.portable.run.v1",
        "tradegrad.portable.final-evaluation.v1",
    ],
)
def test_schema_dispatch_calls_the_correct_admission_loader_before_absence(
    schema, monkeypatch
):
    """Explicit session stub proves dispatch, not database ownership/fencing."""
    import importlib
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from backend.app.trace_services import LOADERS, load_owned_trace

    evaluation = SimpleNamespace(id=uuid4(), attempt_id=uuid4(), payload={})
    run = SimpleNamespace(id=uuid4(), config={"schema_version": schema})
    result = SimpleNamespace(artifact=SimpleNamespace(sha256="e" * 64))
    session = MagicMock()
    session.execute.return_value.one_or_none.return_value = (evaluation, run)
    session.scalar.return_value = None
    owner = uuid4()
    selected = MagicMock(return_value=result)
    module, name = LOADERS[schema]
    for target_module, target_name in LOADERS.values():
        monkeypatch.setattr(
            importlib.import_module(target_module),
            target_name,
            MagicMock(side_effect=AssertionError("Wrong schema admission loader")),
        )
    monkeypatch.setattr(importlib.import_module(module), name, selected)
    settings = SimpleNamespace()
    trace = load_owned_trace(session, evaluation.id, owner, settings)
    selected.assert_called_once_with(session, evaluation.id, owner, settings)
    assert trace.context.status == "not_collected"
    assert trace.reader is None


def test_valid_wire_record_larger_than_normal_block_target_is_publishable(
    tmp_path, binding
):
    """An accepted observation must not turn successful economics into failure."""
    from packages.research_trace.transport import (
        CaptureReceiver,
        CaptureWriter,
        iter_capture_records,
    )

    config = CaptureConfig(mode="research")
    receiver = CaptureReceiver(tmp_path, config, source="strategy_emitted")
    writer = CaptureWriter(config, receiver.accept)
    recorder = Recorder(
        code_sha256=binding.code_sha256, config=config, on_record=writer.append
    )
    moment = "2024-01-30T00:00:00+00:00"
    recorder.begin_decision(
        data_event_id="large-frozen-observation",
        event_seq=0,
        scope="portfolio",
        callback_phase="close",
        information_cutoff=moment,
        decision_time=moment,
        market_timezone="UTC",
    )
    recorder.emit(
        "features",
        {
            "features": [
                {
                    "feature_id": "large_value",
                    "value": "x" * (9 * 1024 * 1024),
                    "validity": "valid",
                    "unit": "native",
                    "calculation_window": "already calculated",
                    "information_cutoff": moment,
                }
            ]
        },
    )
    summary = recorder.summary()
    assert summary["decisions_saved"] == summary["detail_rows_saved"] == 1
    writer.finish(summary)
    result = receiver.result()
    stream = iter_capture_records(
        result["research_capture_path"],
        result["research_capture_sha256"],
        result["research_capture_size_bytes"],
        config,
    )
    manifest, artifacts = write_trace_artifacts(
        stream, binding, summary, tmp_path / "published"
    )
    assert manifest.decisions_saved == manifest.detail_rows_saved == 1
    assert artifacts[-1].name == manifest_name(binding.evaluation_id)
    reader = TraceReader(manifest, {item.name: item.path for item in artifacts})
    event = reader.query("events")["items"][0]
    with pytest.raises(TraceStorageError) as error:
        reader.query(
            "features",
            decision_id=event["decision_id"],
            start=date(2024, 1, 30),
            end=date(2024, 1, 31),
        )
    assert error.value.code == "trace_detail_too_large"


def test_nonmonotonic_months_use_bounded_blocks_not_one_file_per_event(
    tmp_path, binding
):
    from packages.research_trace.contracts import DecisionRecord

    source = captured(days=1)
    initial = source.records[0].model_dump(mode="json")
    count = 7000

    def stream():
        for index in range(count):
            day = "2024-01-30" if index % 2 == 0 else "2024-02-02"
            moment = f"{day}T00:00:00Z"
            raw = {
                **initial,
                "decision_id": f"alternating-{index}",
                "data_event_id": f"source-{index}",
                "decision_time": moment,
                "information_cutoff": moment,
            }
            # Original feature cutoff is January30, never in the decision's future.
            yield DecisionRecord.model_validate(raw)

    summary = source.summary()
    summary.update(
        decisions_seen=count,
        decisions_saved=count,
        detail_rows_seen=8 * count,
        detail_rows_saved=8 * count,
    )
    manifest, artifacts = write_trace_artifacts(stream(), binding, summary, tmp_path)
    assert manifest.decisions_saved == count
    assert len(artifacts) < 30
    assert artifacts[-1].size_bytes < 4 * 1024 * 1024
    reader = TraceReader(manifest, {item.name: item.path for item in artifacts})
    page = reader.query(
        "events", start=date(2024, 1, 30), end=date(2024, 1, 31), limit=50
    )
    assert page["total"] == count // 2
    assert len(page["items"]) == 50
