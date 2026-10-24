# ruff: noqa: SIM117
"""Real PostgreSQL comparison API/job/ownership gates with synthetic source bytes.

DuckDB comparison and filesystem publication are real. The source economic
bundles/observations are explicit synthetic boundary fixtures, not engine or
browser certification. Missing PostgreSQL is never replaced with SQLite.
"""

import copy
import os
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.comparison_guard import comparison_attempt_guard
from backend.app.comparison_schemas import ComparisonCreate
from backend.app.comparison_services import submit_comparison
from backend.app.models import (
    Artifact,
    Comparison,
    ComparisonSubmission,
    DatasetVersion,
    DataUsageExposure,
    Evaluation,
    ExecutionAttempt,
    OutboxEvent,
    ResearchRun,
    RunEvent,
    utcnow,
)
from backend.app.research_services import begin_evaluation, complete_evaluation
from backend.app.services import (
    FenceLost,
    claim_attempt,
    mark_running,
    reconcile_attempt,
)
from backend.tests.research_view_fixtures import synthetic_result
from packages.research_trace.binding import make_binding
from packages.research_trace.storage import write_trace_artifacts
from packages.tradegrad_adapter.config import RunConfig, evaluation_windows
from tests.integration import test_research_lifecycle as lifecycle
from tests.research_trace.test_storage import captured
from workers.research.comparison import execute_comparison

base_research_payload = lifecycle.research_payload
submit_research = lifecycle.submit_research
research_attempt = lifecycle.research_attempt
pytestmark = pytest.mark.postgres


@pytest.fixture
def research_payload(base_research_payload):
    return {**base_research_payload, "research_capture": {"mode": "research"}}


def test_comparison_required_postgres_gate():
    if os.environ.get("TRADEGRAD_COMPARISON_POSTGRES_REQUIRED") == "1":
        assert os.environ.get("TEST_DATABASE_URL"), (
            "Required comparison API/recovery acceptance needs real PostgreSQL TEST_DATABASE_URL"
        )


@pytest.fixture
def published_pair(
    migrated_engine, settings, owner, research_payload, research_attempt
):
    """Two distinct immutable leaves, train/validation, in one real owned run."""
    config = RunConfig.model_validate(research_payload)
    attempt = research_attempt
    values = []
    for window in evaluation_windows(config):
        with Session(migrated_engine, expire_on_commit=False) as session:
            item = begin_evaluation(
                session,
                attempt.id,
                1,
                attempt.lease_token,
                candidate_key="candidate-0000",
                code_sha256=config.seed_strategy.sha256,
                window=window,
            )
            dataset = session.get(DatasetVersion, UUID(config.dataset.id))
            run = session.get(ResearchRun, attempt.run_id)
            result = synthetic_result(
                config,
                owner_id=owner.user.id,
                run_id=run.id,
                attempt_id=attempt.id,
                evaluation_id=item.id,
                dataset_manifest=dataset.manifest,
                evaluation_window=window,
            )
            session.commit()
        path = settings.storage_root / result.artifact.storage_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(result.content)
        recorder = captured(
            code_hash=config.seed_strategy.sha256,
            begin=date.fromisoformat(window["start"]),
        )
        binding = make_binding(
            run=run,
            evaluation=item,
            config=config,
            bundle=result.bundle,
            result_sha256=result.artifact.sha256,
        )
        manifest, written = write_trace_artifacts(
            recorder.records, binding, recorder.summary(), path.parent
        )
        with Session(migrated_engine, expire_on_commit=False) as session:
            session.add(result.artifact)
            registrations = []
            for source in written:
                row = Artifact(
                    id=uuid4(),
                    run_id=run.id,
                    attempt_id=attempt.id,
                    name=source.name,
                    sha256=source.sha256,
                    size_bytes=source.size_bytes,
                    media_type=source.media_type,
                    storage_key=str(source.path.relative_to(settings.storage_root)),
                )
                session.add(row)
                registrations.append(row)
            session.flush()
            reference = registrations[-1]
            payload = result.evaluation.payload | {
                "trace_manifest": {
                    "artifact_id": str(reference.id),
                    "sha256": reference.sha256,
                    "size_bytes": reference.size_bytes,
                },
                "research_capture": recorder.summary(),
            }
            complete_evaluation(
                session,
                attempt.id,
                1,
                attempt.lease_token,
                item.id,
                outcome=payload,
                sandbox_stopped=True,
            )
            session.commit()
        values.append(
            SimpleNamespace(result=result, trace_artifact=reference, manifest=manifest)
        )
    assert len(values) == 2
    return values


def request_body(values):
    def side(value):
        result = value.result
        return {
            "evaluation_id": str(result.evaluation.id),
            "execution_attempt_id": str(result.evaluation.attempt_id),
            "result_manifest_sha256": result.artifact.sha256,
            "trace_manifest_sha256": value.trace_artifact.sha256,
            "source_kind": "strategy_version",
            "source_id": str(result.run.strategy_version_id),
            "code_sha256": result.evaluation.code_sha256,
        }

    return {"left": side(values[0]), "right": side(values[1])}


def submit(api, headers, values, key="comparison-create", body=None):
    response = api.post(
        "/api/v1/comparisons",
        headers=headers | {"Idempotency-Key": key},
        json=body or request_body(values),
    )
    assert response.status_code == 202, response.text
    return response.json()


@pytest.fixture
def queued_comparison(api, owner_headers, published_pair):
    return submit(api, owner_headers, published_pair)


@pytest.fixture
def completed_comparison(queued_comparison, migrated_engine, settings):
    outcome = execute_comparison(
        queued_comparison["run_id"],
        1,
        settings,
        sessionmaker(migrated_engine, expire_on_commit=False),
    )
    assert outcome == {"status": "succeeded", "query_stopped": True}
    return queued_comparison


def test_comparison_is_async_business_object_without_fake_evaluation(
    api, owner_headers, queued_comparison, migrated_engine
):
    value = queued_comparison
    assert value["status"] == "queued" and not value["artifact_ready"]
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, UUID(value["run_id"]))
        assert (
            run.result is None
            and run.config["schema_version"] == "tradegrad.comparison.v1"
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(Evaluation)
                .where(Evaluation.run_id == run.id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.run_id == run.id)
            )
            == 1
        )
    status = api.get(f"/api/v1/comparisons/{value['id']}", headers=owner_headers)
    assert status.status_code == 200 and "manifest" not in status.json()


def test_success_publishes_only_after_real_duckdb_close_and_records_both_exposures(
    api,
    owner_headers,
    published_pair,
    completed_comparison,
    migrated_engine,
):
    identifier = completed_comparison["id"]
    status = api.get(f"/api/v1/comparisons/{identifier}", headers=owner_headers).json()
    assert status["status"] == "succeeded" and status["artifact_ready"]
    response = api.get(
        f"/api/v1/comparisons/{identifier}/result",
        headers=owner_headers,
        params={"manifest_sha256": status["manifest_sha256"]},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert (
        result["manifest"]["left"]["binding"]["evaluation_id"]
        != result["manifest"]["right"]["binding"]["evaluation_id"]
    )
    assert result["manifest"]["diagnostic_parity_certified"] is False
    section = api.get(
        f"/api/v1/comparisons/{identifier}/sections/events",
        headers=owner_headers,
        params={"manifest_sha256": status["manifest_sha256"], "limit": 1},
    )
    assert section.status_code == 200, section.text
    assert section.json()["comparison_id"] == identifier
    assert len(section.json()["items"]) == 1
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, UUID(completed_comparison["run_id"]))
        assert run.result is None
        attempt = session.get(ExecutionAttempt, run.active_attempt_id)
        assert attempt.sandbox_stopped and attempt.status == "succeeded"
        previews = session.scalars(
            select(DataUsageExposure).where(
                DataUsageExposure.purpose == "result_preview"
            )
        ).all()
        assert {str(value.result.evaluation.id) for value in published_pair} <= {
            row.provenance.get("evaluation_id") for row in previews
        }


def test_cache_hit_and_all_aliases_are_reserved_against_other_run_modes(
    api,
    owner_headers,
    published_pair,
    completed_comparison,
    migrated_engine,
    run_payload,
    research_payload,
):
    first = submit(api, owner_headers, published_pair, "same-input-new-key")
    again = submit(api, owner_headers, published_pair, "same-input-new-key")
    assert first["id"] == again["id"] == completed_comparison["id"]
    for path, body in (("/runs", run_payload), ("/research-runs", research_payload)):
        response = api.post(
            "/api/v1" + path,
            headers=owner_headers | {"Idempotency-Key": "same-input-new-key"},
            json=body,
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "idempotency_conflict"
    changed = copy.deepcopy(request_body(published_pair))
    changed["options"] = {"max_runtime_seconds": 1}
    response = api.post(
        "/api/v1/comparisons",
        headers=owner_headers | {"Idempotency-Key": "same-input-new-key"},
        json=changed,
    )
    assert (
        response.status_code == 409
        and response.json()["code"] == "idempotency_conflict"
    )
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(Comparison)) == 1
        assert (
            session.scalar(select(func.count()).select_from(ComparisonSubmission)) == 2
        )


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize(
    "field",
    [
        "execution_attempt_id",
        "result_manifest_sha256",
        "trace_manifest_sha256",
        "source_id",
        "code_sha256",
    ],
)
def test_each_side_pin_is_admitted_exactly(
    api, owner_headers, published_pair, side, field
):
    body = request_body(published_pair)
    body[side][field] = str(uuid4()) if field.endswith("id") else "0" * 64
    response = api.post(
        "/api/v1/comparisons",
        headers=owner_headers | {"Idempotency-Key": "bad-pin"},
        json=body,
    )
    assert response.status_code in {404, 409}, response.text


def test_foreign_owner_and_manifest_pins_cannot_open_cached_evidence(
    api, owner_headers, other_headers, completed_comparison
):
    identifier = completed_comparison["id"]
    for suffix in ("", "/result", "/sections/events"):
        params = {"manifest_sha256": "0" * 64} if suffix else {}
        response = api.get(
            f"/api/v1/comparisons/{identifier}{suffix}",
            headers=other_headers,
            params=params,
        )
        assert response.status_code == 404
    response = api.get(
        f"/api/v1/comparisons/{identifier}/result",
        headers=owner_headers,
        params={"manifest_sha256": "0" * 64},
    )
    assert (
        response.status_code == 409
        and response.json()["code"] == "comparison_pin_mismatch"
    )


def test_generic_comparison_download_requires_both_original_sources(
    api, owner_headers, completed_comparison, published_pair, settings
):
    value = api.get(
        f"/api/v1/comparisons/{completed_comparison['id']}", headers=owner_headers
    ).json()
    path = f"/api/v1/artifacts/{value['manifest_artifact_id']}/download"
    good = api.get(path, headers=owner_headers)
    assert good.status_code == 200
    right_path = settings.storage_root / published_pair[1].result.artifact.storage_key
    right_path.write_bytes(b"tampered-right-evidence")
    assert api.get(path, headers=owner_headers).status_code == 409
    assert (
        api.get(
            f"/api/v1/comparisons/{value['id']}/result", headers=owner_headers
        ).status_code
        == 409
    )
    response = api.post(
        "/api/v1/comparisons",
        headers=owner_headers | {"Idempotency-Key": "tampered-cache"},
        json=request_body(published_pair),
    )
    assert response.status_code == 409


def test_cancel_before_claim_runs_no_comparison_queries(
    api, owner_headers, queued_comparison, settings, migrated_engine, monkeypatch
):
    import packages.research_trace.comparison as engine

    monkeypatch.setattr(
        engine,
        "compare_evaluations",
        lambda *a, **k: pytest.fail("Cancelled queue launched comparison SQL"),
    )
    response = api.post(
        f"/api/v1/comparisons/{queued_comparison['id']}/cancel", headers=owner_headers
    )
    assert response.status_code == 202 and response.json()["status"] == "cancelled"
    assert execute_comparison(
        queued_comparison["run_id"], 1, settings, sessionmaker(migrated_engine)
    ) == {"status": "duplicate_or_stale"}


def claim_expired(migrated_engine, settings, queued_comparison):
    with Session(migrated_engine, expire_on_commit=False) as session:
        attempt = claim_attempt(
            session, UUID(queued_comparison["run_id"]), 1, "comparison-test", settings
        )
        attempt.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
        return attempt


def test_live_query_guard_blocks_docker_only_recovery_then_fences_late_worker(
    migrated_engine,
    settings,
    queued_comparison,
):
    attempt = claim_expired(migrated_engine, settings, queued_comparison)

    def forbidden(_):
        pytest.fail("Comparison recovery must never rely on Docker stop")

    with comparison_attempt_guard(settings.storage_root, attempt.run_id, attempt.id):
        with Session(migrated_engine) as session:
            assert not reconcile_attempt(
                session, attempt.id, forbidden, settings=settings
            )
            session.commit()
        with Session(migrated_engine) as session:
            current = session.get(ExecutionAttempt, attempt.id)
            assert current.finished_at is None and not current.sandbox_stopped
    with Session(migrated_engine) as session:
        assert reconcile_attempt(session, attempt.id, forbidden, settings=settings)
        session.commit()
    with comparison_attempt_guard(settings.storage_root, attempt.run_id, attempt.id):
        with Session(migrated_engine) as session, pytest.raises(FenceLost):
            mark_running(session, attempt.id, 1, attempt.lease_token)


def test_probe_before_worker_or_crash_before_lock_cannot_start_delayed_query(
    migrated_engine, settings, queued_comparison
):
    attempt = claim_expired(migrated_engine, settings, queued_comparison)
    with Session(migrated_engine) as session:
        assert reconcile_attempt(
            session,
            attempt.id,
            lambda _: pytest.fail("Docker called"),
            settings=settings,
        )
        session.commit()
    with comparison_attempt_guard(settings.storage_root, attempt.run_id, attempt.id):
        with Session(migrated_engine) as session, pytest.raises(FenceLost):
            mark_running(session, attempt.id, 1, attempt.lease_token)


def test_missing_guard_settings_fails_closed_and_repeated_recovery_does_not_duplicate_status(
    migrated_engine, settings, queued_comparison
):
    attempt = claim_expired(migrated_engine, settings, queued_comparison)
    with Session(migrated_engine) as session:
        assert not reconcile_attempt(session, attempt.id, lambda _: True)
        assert not reconcile_attempt(session, attempt.id, lambda _: True)
        session.commit()
    with Session(migrated_engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(RunEvent)
                .where(
                    RunEvent.run_id == attempt.run_id,
                    RunEvent.type == "recovery_blocked",
                )
            )
            == 1
        )


def test_submission_rollback_leaves_no_comparison_alias_run_or_outbox(
    migrated_engine, settings, owner, published_pair
):
    with Session(migrated_engine) as session:
        item, run = submit_comparison(
            session,
            owner.user.id,
            ComparisonCreate.model_validate(request_body(published_pair)),
            "rollback-comparison",
            settings,
        )
        identifier, run_id = item.id, run.id
        session.rollback()
    with Session(migrated_engine) as session:
        assert (
            session.get(Comparison, identifier) is None
            and session.get(ResearchRun, run_id) is None
        )
        assert (
            session.get(ComparisonSubmission, (owner.user.id, "rollback-comparison"))
            is None
        )


@pytest.mark.parametrize("same_key", [False, True])
def test_concurrent_submission_serializes_input_cache_and_durable_aliases(
    migrated_engine, settings, owner, published_pair, same_key
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    start = Barrier(2)
    body = ComparisonCreate.model_validate(request_body(published_pair))

    def create(index):
        with Session(migrated_engine) as session:
            start.wait(timeout=10)
            item, run = submit_comparison(
                session,
                owner.user.id,
                body,
                "concurrent" if same_key else f"concurrent-{index}",
                settings,
            )
            result = (item.id, run.id)
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert results[0] == results[1]
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(Comparison)) == 1
        assert session.scalar(
            select(func.count()).select_from(ComparisonSubmission)
        ) == (1 if same_key else 2)
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.run_id == results[0][1])
            )
            == 1
        )


@pytest.mark.parametrize(
    "change", ["input", "owner", "delete", "alias_update", "alias_delete"]
)
def test_database_rejects_rewriting_comparison_inputs_or_submission_receipts(
    migrated_engine, queued_comparison, other, change
):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    statements = {
        "input": "UPDATE comparisons SET input_sha256 = repeat('0', 64) WHERE id = :id",
        "owner": "UPDATE comparisons SET owner_id = :owner WHERE id = :id",
        "delete": "DELETE FROM comparisons WHERE id = :id",
        "alias_update": "UPDATE comparison_submissions SET request_sha256 = repeat('0', 64) WHERE comparison_id = :id",
        "alias_delete": "DELETE FROM comparison_submissions WHERE comparison_id = :id",
    }
    with migrated_engine.begin() as connection, pytest.raises(DBAPIError):
        connection.execute(
            text(statements[change]),
            {"id": UUID(queued_comparison["id"]), "owner": other.user.id},
        )


def _completed_engine_output(
    migrated_engine, settings, queued_comparison, guard, attempt
):
    from backend.app.comparison_services import validate_comparison_inputs
    from packages.research_trace.comparison import (
        ComparisonControl,
        compare_evaluations,
    )
    from packages.research_trace.comparison_contracts import ComparisonPublication

    with Session(migrated_engine) as session:
        assert mark_running(session, attempt.id, 1, attempt.lease_token)
        run = session.get(ResearchRun, attempt.run_id)
        item = session.get(Comparison, UUID(queued_comparison["id"]))
        sides = validate_comparison_inputs(session, item, run, settings)
        session.commit()
    control = ComparisonControl()
    result = compare_evaluations(
        sides[0].input,
        sides[1].input,
        guard.path / "artifacts",
        publication=ComparisonPublication(
            comparison_id=item.id, run_id=run.id, execution_attempt_id=attempt.id
        ),
        control=control,
        durable_root=settings.storage_root,
    )
    control.close()
    assert control.closure_confirmed
    return result


def _claim(migrated_engine, settings, queued_comparison):
    with Session(migrated_engine, expire_on_commit=False) as session:
        attempt = claim_attempt(
            session, UUID(queued_comparison["run_id"]), 1, "publication-race", settings
        )
        session.commit()
        return attempt


@pytest.mark.parametrize("first", ["cancel", "publication"])
def test_cancellation_and_publication_serialize_on_real_run_lock(
    migrated_engine, settings, owner, queued_comparison, first
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from backend.app.comparison_services import finish_comparison_attempt
    from backend.app.services import request_cancel

    attempt = _claim(migrated_engine, settings, queued_comparison)
    with comparison_attempt_guard(
        settings.storage_root, attempt.run_id, attempt.id
    ) as guard:
        manifest, artifacts = _completed_engine_output(
            migrated_engine, settings, queued_comparison, guard, attempt
        )
        with Session(migrated_engine) as winning:
            if first == "cancel":
                request_cancel(winning, attempt.run_id, owner.user.id)
            else:
                finish_comparison_attempt(
                    winning,
                    attempt.id,
                    1,
                    attempt.lease_token,
                    guard=guard,
                    settings=settings,
                    status="succeeded",
                    manifest=manifest,
                    artifacts=artifacts,
                )
            entered = Event()

            def second():
                with Session(migrated_engine) as losing:
                    entered.set()
                    if first == "cancel":
                        finish_comparison_attempt(
                            losing,
                            attempt.id,
                            1,
                            attempt.lease_token,
                            guard=guard,
                            settings=settings,
                            status="succeeded",
                            manifest=manifest,
                            artifacts=artifacts,
                        )
                    else:
                        request_cancel(losing, attempt.run_id, owner.user.id)
                    losing.commit()

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(second)
                assert entered.wait(timeout=5)
                assert not future.done()
                winning.commit()
                future.result(timeout=15)
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, attempt.run_id)
        item = session.get(Comparison, UUID(queued_comparison["id"]))
        assert run.status == ("cancelled" if first == "cancel" else "succeeded")
        assert (item.manifest_artifact_id is None) == (first == "cancel")
        assert session.get(ExecutionAttempt, attempt.id).sandbox_stopped


def test_completion_rollback_and_expired_refence_never_register_partial_output(
    migrated_engine, settings, queued_comparison
):
    from backend.app.comparison_services import finish_comparison_attempt

    attempt = _claim(migrated_engine, settings, queued_comparison)
    with comparison_attempt_guard(
        settings.storage_root, attempt.run_id, attempt.id
    ) as guard:
        manifest, artifacts = _completed_engine_output(
            migrated_engine, settings, queued_comparison, guard, attempt
        )
        with Session(migrated_engine) as session:
            finish_comparison_attempt(
                session,
                attempt.id,
                1,
                attempt.lease_token,
                guard=guard,
                settings=settings,
                status="succeeded",
                manifest=manifest,
                artifacts=artifacts,
            )
            session.rollback()
        with Session(migrated_engine) as session:
            item = session.get(Comparison, UUID(queued_comparison["id"]))
            assert item.manifest_artifact_id is None
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(Artifact)
                    .where(Artifact.run_id == attempt.run_id)
                )
                == 0
            )
            session.get(ExecutionAttempt, attempt.id).lease_expires_at = (
                utcnow() - timedelta(seconds=1)
            )
            session.commit()
        with Session(migrated_engine) as session, pytest.raises(FenceLost):
            finish_comparison_attempt(
                session,
                attempt.id,
                1,
                attempt.lease_token,
                guard=guard,
                settings=settings,
                status="succeeded",
                manifest=manifest,
                artifacts=artifacts,
            )
    with Session(migrated_engine) as session:
        assert reconcile_attempt(
            session,
            attempt.id,
            lambda _: pytest.fail("Docker called"),
            settings=settings,
        )
        session.commit()
        assert (
            session.get(Comparison, UUID(queued_comparison["id"])).manifest_artifact_id
            is None
        )


def _orphaned_query_process(
    storage_root, run_id, attempt_id, connection_parameters, ready
):
    """Owned child, real DB disconnect + real synchronous DuckDB, no fake stop."""
    import duckdb
    from sqlalchemy import create_engine, event
    from sqlalchemy.pool import NullPool

    from backend.app.core.maintenance import shared_lifecycle

    engine = create_engine(
        connection_parameters[0],
        poolclass=NullPool,
        connect_args=connection_parameters[1],
    )
    connections = []
    event.listen(engine, "engine_connect", connections.append)
    with shared_lifecycle(engine, storage_root):
        with comparison_attempt_guard(storage_root, run_id, attempt_id):
            # Drop the actual global lifecycle PostgreSQL connection. Its
            # session advisory lock is gone; both filesystem guards survive.
            connections[0].invalidate()
            with duckdb.connect(":memory:") as query:
                announced = False

                def active_query():
                    nonlocal announced
                    if not announced:
                        ready.send("guard-held-after-db-loss")
                        announced = True
                    return 0

                query.create_function(
                    "active_query", active_query, [], "INTEGER", side_effects=True
                )
                query.execute(
                    "SELECT sum(sin(i) + active_query()) FROM range(1000000000000) t(i)"
                )
    engine.dispose()


def test_live_query_after_database_loss_blocks_recovery_until_owned_process_sigkill(
    migrated_engine, settings, queued_comparison
):
    import multiprocessing
    import signal

    from sqlalchemy import text

    from backend.app.core.maintenance import MaintenanceBusy
    from backend.app.core.storage_barrier import storage_guard

    attempt = claim_expired(migrated_engine, settings, queued_comparison)
    with migrated_engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(
        target=_orphaned_query_process,
        args=(
            settings.storage_root,
            attempt.run_id,
            attempt.id,
            (
                migrated_engine.url.render_as_string(hide_password=False),
                {"connect_timeout": 5, "options": f"-csearch_path={schema}"},
            ),
            send,
        ),
    )
    process.start()
    send.close()
    try:
        assert receive.poll(15) and receive.recv() == "guard-held-after-db-loss"
        for _ in range(2):
            with Session(migrated_engine) as session:
                assert not reconcile_attempt(
                    session,
                    attempt.id,
                    lambda _: pytest.fail("Docker called"),
                    settings=settings,
                )
                session.commit()
        with pytest.raises(MaintenanceBusy):
            with storage_guard(settings.storage_root, exclusive=True):
                pytest.fail("Cold maintenance bypassed orphaned live query")
        process.kill()
        process.join(timeout=10)
        assert process.exitcode == -signal.SIGKILL
        with Session(migrated_engine) as session:
            assert reconcile_attempt(
                session,
                attempt.id,
                lambda _: pytest.fail("Docker called"),
                settings=settings,
            )
            session.commit()
            row = session.get(ExecutionAttempt, attempt.id)
            assert (
                row.finished_at
                and row.sandbox_stopped
                and row.lease_token != attempt.lease_token
            )
        with comparison_attempt_guard(
            settings.storage_root, attempt.run_id, attempt.id
        ):
            with Session(migrated_engine) as session, pytest.raises(FenceLost):
                mark_running(session, attempt.id, 1, attempt.lease_token)
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
        receive.close()


def test_retry_requires_confirmed_stop_and_reserves_only_one_new_generation(
    api, owner_headers, settings, migrated_engine, queued_comparison
):
    attempt = claim_expired(migrated_engine, settings, queued_comparison)
    base = f"/api/v1/comparisons/{queued_comparison['id']}"
    headers = owner_headers | {"Idempotency-Key": "retry-once"}
    with comparison_attempt_guard(settings.storage_root, attempt.run_id, attempt.id):
        assert api.post(base + "/retry", headers=headers).status_code == 409
        with Session(migrated_engine) as session:
            assert not reconcile_attempt(
                session, attempt.id, lambda _: True, settings=settings
            )
            session.commit()
    with Session(migrated_engine) as session:
        assert reconcile_attempt(session, attempt.id, lambda _: True, settings=settings)
        session.commit()
    first, second = (
        api.post(base + "/retry", headers=headers),
        api.post(base + "/retry", headers=headers),
    )
    assert first.status_code == second.status_code == 202
    assert first.json()["generation"] == second.json()["generation"] == 2
    assert not first.json()["artifact_ready"] and "manifest" not in first.json()
    with Session(migrated_engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.run_id == attempt.run_id)
            )
            == 2
        )
        assert (
            session.get(ExecutionAttempt, attempt.id).lease_token != attempt.lease_token
        )


def test_comparison_sse_strips_persisted_summary_and_never_uses_single_source_exposure(
    api, owner_headers, migrated_engine, completed_comparison
):
    from backend.app.services import append_event

    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, UUID(completed_comparison["run_id"]))
        append_event(
            session,
            run,
            "private-source-evidence",
            {"code": "private_strategy_source", "metrics": {"return": 999}},
        )
        append_event(
            session,
            run,
            "run_finished",
            {
                "status": "succeeded",
                "result": {"private": "private_strategy_source"},
                "error": "raw_sql_or_source",
            },
        )
        before = session.scalar(select(func.count()).select_from(DataUsageExposure))
        session.commit()
    response = api.get(
        f"/api/v1/runs/{completed_comparison['run_id']}/events",
        headers=owner_headers,
        params={"follow": "false"},
    )
    assert response.status_code == 200, response.text
    assert "private_strategy_source" not in response.text
    assert (
        "raw_sql_or_source" not in response.text
        and "private-source-evidence" not in response.text
    )
    assert (
        "comparison_status" in response.text
        and "comparison_artifact_ready" in response.text
    )
    with Session(migrated_engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(DataUsageExposure))
            == before
        )


def test_comparison_migration_downgrade_refuses_evidence_loss(
    migrated_engine, queued_comparison
):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy.exc import DBAPIError

    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "backend/alembic.ini"))
    config.set_main_option("script_location", str(root / "backend/alembic"))
    with pytest.raises(DBAPIError), migrated_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "007_research_specs")
    with Session(migrated_engine) as session:
        assert session.get(Comparison, UUID(queued_comparison["id"])) is not None
