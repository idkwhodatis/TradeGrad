"""Real PostgreSQL original-final and new-generation independent admission gates.

Final bundles are explicitly synthetic, using the existing strict native and
portable final-completion fixtures, not mocked result loaders. The historical
portable fixture has no supported current environment/data, so dispatch remains
unsupported. Its writer-only transaction is rolled back, never queued or run.
"""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from backend.app.independent_schemas import DiagnosticReplayCreate
from backend.app.independent_services import (
    _make_config,
    _selected_strategy,
    record_independent_admission,
    require_independent_admission,
    resolve_source,
)
from backend.app.models import (
    Artifact,
    DataUsageExposure,
    Evaluation,
    ExecutionAttempt,
    IndependentEvaluation,
    OutboxEvent,
    ResearchRun,
)
from backend.app.services import DomainError
from packages.research_trace.contracts import digest
from packages.tradegrad_adapter.independent_config import IndependentSourcePin
from tests.integration import test_comparison_portable_api as portable
from tests.integration import test_strategy_packages as packages

research_payload = packages.research_payload
submit_research = packages.submit_research
research_attempt = packages.research_attempt
publish_initial = packages.publish_initial
complete_candidate = packages.complete_candidate
completed_source = packages.completed_source
frozen_selection = packages.frozen_selection
package_reference = packages.package_reference
portable_final_pair = portable.portable_final_pair
pytestmark = pytest.mark.postgres


@pytest.fixture(params=["native", "portable"])
def final_source(request, migrated_engine):
    if request.param == "portable":
        pair = request.getfixturevalue("portable_final_pair")
        return SimpleNamespace(
            mode="portable", pin=pair.body["left"], run_id=pair.refs[0].run_id
        )
    ref = request.getfixturevalue("package_reference")
    with Session(migrated_engine) as session:
        evaluation = session.get(Evaluation, ref.evaluation_id)
        run = session.get(ResearchRun, ref.run_id)
        artifact = session.get(
            Artifact, UUID(evaluation.payload["result_bundle"]["artifact_id"])
        )
        return SimpleNamespace(
            mode="native",
            run_id=run.id,
            pin={
                "evaluation_id": str(evaluation.id),
                "execution_attempt_id": str(evaluation.attempt_id),
                "result_manifest_sha256": artifact.sha256,
                "trace_manifest_sha256": None,
                "source_kind": "strategy_version",
                "source_id": str(run.strategy_version_id),
                "code_sha256": evaluation.code_sha256,
            },
        )


def diagnostic(api, headers, source):
    return api.post(
        f"/api/v1/evaluations/{source.pin['evaluation_id']}/diagnostic-replays",
        headers=headers | {"Idempotency-Key": uuid4().hex},
        json={"source": source.pin},
    )


def test_final_source_controls_forbidden_and_historical_environment_not_upgraded(
    api, owner_headers, final_source
):
    capability = api.post(
        "/api/v1/independent-evaluations/capability",
        headers=owner_headers,
        json=final_source.pin,
    )
    assert capability.status_code == 200, capability.text
    value = capability.json()
    assert (
        not value["can_control"]
        and "controlled_holdout_forbidden" in value["control_unavailable_reasons"]
    )
    assert value["parity_prerequisites"]["status"] == "not_verifiable"
    assert value["exposure_snapshot"]["independent_unseen_holdout_established"] is False
    control = api.post(
        "/api/v1/controlled-experiments",
        headers=owner_headers | {"Idempotency-Key": uuid4().hex},
        json={
            "source": final_source.pin,
            "name": "Forbidden final controls",
            "hypothesis": "No holdout tuning",
            "planned_change": "Must reject before arm creation",
            "arms": [
                {"name": "baseline", "code": value["source_code"]},
                {"name": "same", "code": value["source_code"]},
            ],
        },
    )
    assert (
        control.status_code == 422
        and control.json()["code"] == "controlled_source_unavailable"
    )
    response = diagnostic(api, owner_headers, final_source)
    if final_source.mode == "native":
        assert response.status_code == 202, response.text
        assert response.json()["config"]["split"] == "holdout"
    else:
        assert not value["can_execute"]
        assert (
            "source_environment_not_supported_by_current_runtime"
            in value["unavailable_reasons"]
        )
        assert (
            response.status_code == 422
            and response.json()["code"] == "independent_source_unavailable"
        )


@pytest.mark.parametrize(
    "fault",
    [
        "pending",
        "unfinished_attempt",
        "wrong_publication",
        "new_generation_without_admission",
    ],
)
def test_final_source_readmission_denies_diagnostic_before_new_run(
    api, owner_headers, migrated_engine, final_source, fault
):
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, final_source.run_id)
        attempt = session.get(ExecutionAttempt, run.active_attempt_id)
        if fault == "pending":
            run.status, run.result = "running", None
        elif fault == "unfinished_attempt":
            attempt.finished_at = None
        elif fault == "wrong_publication":
            run.result = {"evaluation_id": str(uuid4())}
        else:
            run.generation += 1
        session.commit()
    response = diagnostic(api, owner_headers, final_source)
    assert response.status_code == 409, response.text
    with Session(migrated_engine) as session:
        assert session.scalar(select(IndependentEvaluation.id).limit(1)) is None


def test_original_final_admission_cannot_be_revoked_after_source_publication(
    migrated_engine, final_source
):
    with Session(migrated_engine) as session:
        admission = session.scalar(
            select(DataUsageExposure).where(
                DataUsageExposure.source_key == f"final:{final_source.run_id}:1"
            )
        )
        assert (
            admission is not None
            and admission.provenance["admission_before_dispatch"] is True
        )
        session.delete(admission)
        with pytest.raises(DBAPIError, match="immutable"):
            session.commit()
    with Session(migrated_engine) as session:
        assert (
            session.scalar(
                select(DataUsageExposure.id).where(
                    DataUsageExposure.source_key == f"final:{final_source.run_id}:1"
                )
            )
            is not None
        )


def test_new_holdout_admission_writer_binds_exact_generation_source_and_prefix(
    migrated_engine, settings, owner, final_source
):
    # A real transaction exercises both schema families and the writer/reader
    # gate. No OutboxEvent is created; historical portable execution is NOT
    # admitted by this test, and the entire staged metadata graph is rolled back.
    body = DiagnosticReplayCreate(
        source=IndependentSourcePin.model_validate(final_source.pin)
    )
    with Session(migrated_engine) as session:
        source = resolve_source(session, owner.user.id, body.source, settings)
        assert source.result.evaluation.split == "holdout"
        strategy = _selected_strategy(
            session,
            owner.user.id,
            source,
            name="Unlaunched admission boundary",
            code=source.admitted.source.code,
            parameters=source.parameters,
        )
        identifier = uuid4()
        config = _make_config(source, strategy, identifier, "diagnostic", body)
        run = ResearchRun(
            id=uuid4(),
            owner_id=owner.user.id,
            idempotency_key=uuid4().hex,
            request_sha256=config.sha256,
            config=config.model_dump(mode="json"),
            dataset_version_id=source.result.dataset.id,
            strategy_version_id=strategy.id,
            experiment_id=source.result.run.experiment_id,
            split_version_id=source.result.run.split_version_id,
            research_image_digest=config.research_image_digest,
            portable_policy_image_digest=getattr(
                config, "portable_policy_image_digest", None
            ),
            status="queued",
            generation=1,
        )
        session.add(run)
        session.flush()
        item = IndependentEvaluation(
            id=identifier,
            owner_id=owner.user.id,
            run_id=run.id,
            source_evaluation_id=body.source.evaluation_id,
            purpose="diagnostic",
            request=body.model_dump(mode="json"),
            request_sha256=body.sha256,
            source_snapshot=source.snapshot,
            source_snapshot_sha256=digest(source.snapshot),
            protocol=source.protocol,
            protocol_sha256=digest(source.protocol),
        )
        session.add(item)
        session.flush()
        for generation in (1, 2):
            run.generation = generation
            session.flush()
            with pytest.raises(DomainError) as missing:
                require_independent_admission(session, run)
            assert missing.value.code == "admission_required"
            record_independent_admission(session, run, item, source)
            session.flush()
            admission = require_independent_admission(session, run)
            assert admission.provenance["generation"] == generation
            assert (
                admission.provenance["source_evaluation_id"]
                == final_source.pin["evaluation_id"]
            )
            assert (
                admission.provenance["source_result_sha256"]
                == final_source.pin["result_manifest_sha256"]
            )
            assert admission.provenance["protocol_sha256"] == config.protocol_sha256
            assert admission.provenance["requested_window"] == config.window
            assert admission.start_date == source.result.dataset.start_date
            assert admission.end_date == config.reconstruction_window.end
            assert admission.experiment_id == run.experiment_id
            assert admission.split_version_id == run.split_version_id
        assert (
            session.scalar(select(OutboxEvent.id).where(OutboxEvent.run_id == run.id))
            is None
        )
        session.rollback()
