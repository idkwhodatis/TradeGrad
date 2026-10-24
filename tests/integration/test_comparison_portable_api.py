"""Real PostgreSQL portable-final bilateral comparison admission boundaries.

Historical source selection/checkpoint rows and broker observations are explicitly
synthetic. Final admission, physical completion, strict loaders, DuckDB comparison,
HTTP ownership and exposure writes are real. No candidate executes here; this is
not optimizer/freeze, native-engine, Docker or browser certification.
"""

import copy
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core.atomic_storage import publish_bytes
from backend.app.experiment_schemas import SplitDefinition
from backend.app.experiment_services import portable_selected_metadata
from backend.app.models import (
    Artifact,
    Candidate,
    DatasetVersion,
    DataUsageExposure,
    ExecutionAttempt,
    Experiment,
    FinalSelection,
    ResearchCheckpoint,
    ResearchRun,
    SplitVersion,
    StrategyVersion,
    UniverseVersion,
    utcnow,
)
from backend.app.portable_final_services import (
    begin_final_evaluation,
    complete_final_evaluation,
    finish_final_attempt,
    record_final_admission,
)
from backend.app.portable_final_views import load_owned_final_result
from backend.app.portable_services import portable_score, verify_portable_bundle_binding
from backend.app.services import claim_attempt
from packages.strategy_sdk import hash_json
from packages.tradegrad_adapter.portable_config import PortableRunConfig
from packages.tradegrad_adapter.portable_final_config import (
    PortableFinalEvaluationConfig,
)
from packages.tradegrad_adapter.portable_prompts import template_source
from packages.tradegrad_adapter.portable_results import canonical_json_bytes
from packages.tradegrad_adapter.strategy_package import sha256
from tests.portable.test_engine import fixture as engine_fixture
from tests.portable.test_engine import hold_bundle
from workers.research.comparison import execute_comparison

pytestmark = pytest.mark.postgres
IMAGES = {"engine": "sha256:" + "a" * 64, "policy": "sha256:" + "b" * 64}


def source_values(owner_id):
    """Pure exact data construction. Reviewed source is bytes only, never imported."""
    request, market = engine_fixture()
    bundle = hold_bundle()
    code = template_source("portfolio")
    params = {"top_n": 1}
    manifest = copy.deepcopy(bundle["manifest"])
    manifest.update(
        code_sha256=sha256(code.encode()), parameters_sha256=hash_json(params)
    )
    dataset_manifest = {
        "schema_version": "synthetic-comparison-boundary.v1",
        "is_synthetic": True,
        "listings": market["listings"],
        "universe": {"state": "user_fixed"},
        "files": {},
        "artifact_files": {},
    }
    dataset = DatasetVersion(
        id=uuid4(),
        owner_id=owner_id,
        name="Synthetic portable comparison boundary",
        market="US",
        start_date=date(2024, 1, 1),
        end_date=date(2025, 1, 7),
        source="synthetic_fixture",
        is_synthetic=True,
        content_sha256=hash_json(dataset_manifest),
        manifest=dataset_manifest,
        storage_key=f"datasets/{uuid4()}",
    )
    universe = UniverseVersion(
        id=uuid4(),
        owner_id=owner_id,
        name="Synthetic fixed basket",
        state="user_fixed",
        as_of=utcnow(),
        members=market["listings"],
        evidence=[],
        content_sha256=manifest["universe"]["snapshot_sha256"],
    )
    seed = StrategyVersion(
        id=UUID(manifest["strategy_id"]),
        owner_id=owner_id,
        name="Synthetic source",
        task="us_cross",
        mode="portable",
        code=code,
        content_sha256=sha256(code.encode()),
        manifest=manifest,
        parameters=params,
    )
    source_config = PortableRunConfig(
        execution_mode="optimization",
        task="us_cross",
        strategy_kind="portfolio",
        dataset={"id": str(dataset.id), "sha256": dataset.content_sha256},
        universe={"id": str(universe.id), "sha256": universe.content_sha256},
        seed_strategy={"id": str(seed.id), "sha256": seed.content_sha256},
        seed_manifest={"id": f"{seed.id}/manifest", "sha256": hash_json(manifest)},
        seed_parameters={"id": f"{seed.id}/parameters", "sha256": hash_json(params)},
        windows={
            "train": {"start": "2024-01-01", "end": "2024-06-01"},
            "validation": {"start": "2024-06-01", "end": "2024-12-01"},
            "holdout": {key: bundle["window"][key] for key in ("start", "end")},
        },
        warmup={"sessions": 3},
        initial_cash=request["initial_cash"],
        initial_state=request["initial_state"],
        environment_digest=manifest["engine_profile"]["environment_sha256"],
    )
    experiment = Experiment(id=uuid4(), owner_id=owner_id, name="Synthetic history")
    definition = SplitDefinition(
        dataset=source_config.dataset,
        universe=source_config.universe,
        windows=source_config.windows,
    )
    split = SplitVersion(
        id=uuid4(),
        owner_id=owner_id,
        experiment_id=experiment.id,
        dataset_version_id=dataset.id,
        universe_version_id=universe.id,
        definition=definition.model_dump(mode="json"),
        content_sha256=definition.sha256,
    )
    source = ResearchRun(
        id=uuid4(),
        owner_id=owner_id,
        idempotency_key=uuid4().hex,
        request_sha256=source_config.sha256,
        config=source_config.model_dump(mode="json"),
        dataset_version_id=dataset.id,
        strategy_version_id=seed.id,
        experiment_id=experiment.id,
        split_version_id=split.id,
        status="succeeded",
        generation=1,
        research_image_digest=IMAGES["engine"],
        portable_policy_image_digest=IMAGES["policy"],
    )
    attempt = ExecutionAttempt(
        id=uuid4(),
        run_id=source.id,
        generation=1,
        worker_id="synthetic-history",
        lease_expires_at=utcnow() + timedelta(minutes=1),
        status="succeeded",
        sandbox_id=f"synthetic-history-{source.id}",
        sandbox_stopped=True,
        finished_at=utcnow(),
        exit_code=0,
    )
    source.active_attempt_id = attempt.id
    candidate = Candidate(
        id=uuid4(),
        run_id=source.id,
        attempt_id=attempt.id,
        generation=1,
        candidate_key="candidate-0000",
        kind="seed",
        iteration=0,
        repair_index=0,
        code=code,
        code_sha256=seed.content_sha256,
    )
    checkpoint = ResearchCheckpoint(
        id=uuid4(),
        run_id=source.id,
        attempt_id=attempt.id,
        generation=1,
        sequence=1,
        schema_version=1,
        manifest_storage_key=f"synthetic-history/{source.id}.json",
        manifest_sha256=hash_json({"synthetic_checkpoint": str(source.id)}),
        input_digest=hash_json({"synthetic_input": str(source.id)}),
        config_sha256=source_config.sha256,
        summary={
            "candidates": {
                candidate.candidate_key: {
                    "id": str(candidate.id),
                    "valid": True,
                    "complete": True,
                }
            }
        },
        next_stage="finished",
        iteration=0,
        complete=True,
    )
    selected_manifest, selected_params = portable_selected_metadata(
        source_config, seed, candidate
    )
    selected = StrategyVersion(
        id=uuid4(),
        owner_id=owner_id,
        name="Synthetic frozen selected source",
        task="us_cross",
        mode="portable",
        code=code,
        content_sha256=seed.content_sha256,
        parent_id=seed.id,
        manifest=selected_manifest,
        parameters=selected_params,
    )
    selection = FinalSelection(
        id=uuid4(),
        owner_id=owner_id,
        experiment_id=experiment.id,
        split_version_id=split.id,
        source_run_id=source.id,
        source_candidate_id=candidate.id,
        source_checkpoint_id=checkpoint.id,
        strategy_version_id=selected.id,
        request_sha256=hash_json({"synthetic_selection": str(source.id)}),
        source_config_sha256=source_config.sha256,
        source_code_sha256=seed.content_sha256,
        source_checkpoint_sha256=checkpoint.manifest_sha256,
        selection_evidence={"synthetic_fixture": True},
        exposure_snapshot={},
    )
    config = PortableFinalEvaluationConfig(
        selection_id=selection.id,
        split_version_id=split.id,
        split_sha256=split.content_sha256,
        selected_strategy={"id": str(selected.id), "sha256": selected.content_sha256},
        selected_manifest={
            "id": f"{selected.id}/manifest",
            "sha256": hash_json(selected_manifest),
        },
        selected_parameters={
            "id": f"{selected.id}/parameters",
            "sha256": hash_json(selected_params),
        },
        source_config=source_config,
        source_config_sha256=source_config.sha256,
        holdout=source_config.windows.holdout,
        environment_digest=source_config.environment_digest,
        max_runtime_seconds=300,
    )
    bundle["manifest"] = selected_manifest
    bundle["identity"].update(
        strategy_sha256=selected.content_sha256,
        manifest_sha256=hash_json(selected_manifest),
        parameters_sha256=hash_json(selected_params),
        run_config_sha256=config.sha256,
    )
    return SimpleNamespace(**locals())


def test_synthetic_portable_comparison_fixture_has_exact_contract_without_execution():
    value = source_values(uuid4())
    evaluation = SimpleNamespace(
        code_sha256=value.selected.content_sha256,
        window={"split": "holdout", **value.config.holdout.model_dump(mode="json")},
    )
    assert (
        verify_portable_bundle_binding(value.bundle, value.config, evaluation)
        == value.bundle
    )


@pytest.fixture
def portable_final_pair(migrated_engine, settings, owner):
    value = source_values(owner.user.id)
    sides, references = [], []
    with Session(migrated_engine, expire_on_commit=False) as session:
        # Explicit flush ordering exercises real foreign keys and ownership triggers.
        for group in (
            [value.dataset, value.universe, value.seed, value.experiment],
            [value.split],
            [value.source],
            [value.attempt],
            [value.candidate, value.checkpoint],
            [value.selected],
            [value.selection],
        ):
            session.add_all(group)
            session.flush()
        session.commit()
        for _ in range(2):
            config = value.config
            run = ResearchRun(
                id=uuid4(),
                owner_id=owner.user.id,
                idempotency_key=uuid4().hex,
                request_sha256=config.sha256,
                config=config.model_dump(mode="json"),
                dataset_version_id=value.dataset.id,
                strategy_version_id=value.selected.id,
                experiment_id=value.experiment.id,
                split_version_id=value.split.id,
                final_selection_id=value.selection.id,
                generation=1,
                status="queued",
                research_image_digest=IMAGES["engine"],
                portable_policy_image_digest=IMAGES["policy"],
            )
            session.add(run)
            session.flush()
            record_final_admission(
                session, run, value.selection, value.split, value.dataset
            )
            session.commit()
            attempt = claim_attempt(
                session, run.id, 1, "portable-pg-boundary", settings
            )
            assert attempt is not None
            session.commit()
            evaluation = begin_final_evaluation(
                session, attempt.id, 1, attempt.lease_token
            )
            session.commit()
            content = canonical_json_bytes(value.bundle)
            artifact = Artifact(
                id=uuid4(),
                run_id=run.id,
                attempt_id=attempt.id,
                name=f"evaluation-{evaluation.id}-portable-result.json",
                media_type="application/json",
                sha256=sha256(content),
                size_bytes=len(content),
                storage_key=f"portable-comparison-fixture/{evaluation.id}/result.json",
            )
            publish_bytes(
                settings.storage_root / artifact.storage_key,
                content,
                sha256=artifact.sha256,
            )
            session.add(artifact)
            session.flush()
            outcome = {
                "status": "succeeded",
                "exit_code": 0,
                "image_digest": IMAGES["engine"],
                "policy_image_digest": IMAGES["policy"],
                "metrics": value.bundle["summary"],
                "combined_score": portable_score(value.bundle),
                "result_trust": value.bundle["result_trust"],
                "ledger_origin": "trusted_engine",
                "runtime_certified": False,
                "portable_result": {
                    "artifact_id": str(artifact.id),
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                },
            }
            completed = complete_final_evaluation(
                session,
                attempt.id,
                1,
                attempt.lease_token,
                evaluation.id,
                outcome=outcome,
                sandbox_stopped=True,
                settings=settings,
            )
            assert completed.status == "succeeded"
            finished = finish_final_attempt(
                session,
                attempt.id,
                1,
                attempt.lease_token,
                {
                    "status": "succeeded",
                    "exit_code": 0,
                    "evaluation_id": str(evaluation.id),
                },
                True,
            )
            assert finished.status == "succeeded"
            session.commit()
            # Genuine strict loader, not a fixture replacement, must accept before comparison.
            assert (
                load_owned_final_result(
                    session, evaluation.id, owner.user.id, settings
                ).bundle
                == value.bundle
            )
            session.commit()
            sides.append(
                {
                    "evaluation_id": str(evaluation.id),
                    "execution_attempt_id": str(attempt.id),
                    "result_manifest_sha256": artifact.sha256,
                    "trace_manifest_sha256": None,
                    "source_kind": "strategy_version",
                    "source_id": str(value.selected.id),
                    "code_sha256": value.selected.content_sha256,
                }
            )
            references.append(
                SimpleNamespace(run_id=run.id, evaluation_id=evaluation.id)
            )
    return SimpleNamespace(
        body=dict(zip(("left", "right"), sides, strict=True)), refs=references
    )


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize(
    "fault",
    ["pending", "retry", "unfinished_attempt", "wrong_publication", "selected_source"],
)
def test_portable_final_both_sides_recheck_exact_release_and_exposure(
    api,
    owner_headers,
    other_headers,
    migrated_engine,
    settings,
    portable_final_pair,
    side,
    fault,
):
    pair = portable_final_pair
    headers = owner_headers | {"Idempotency-Key": "portable-final-comparison"}
    response = api.post("/api/v1/comparisons", headers=headers, json=pair.body)
    assert response.status_code == 202, response.text
    comparison = response.json()
    assert (
        execute_comparison(
            comparison["run_id"],
            1,
            settings,
            sessionmaker(migrated_engine, expire_on_commit=False),
        )["status"]
        == "succeeded"
    )
    base = f"/api/v1/comparisons/{comparison['id']}"
    status = api.get(base, headers=owner_headers).json()
    result = api.get(base + "/result", headers=owner_headers)
    assert result.status_code == 200, result.text
    assert result.json()["manifest"][side]["trace_state"] == "historical_absence"
    for path, params in (
        (base + "/result", {}),
        (base + "/sections/events", {"manifest_sha256": status["manifest_sha256"]}),
        (f"/api/v1/artifacts/{status['manifest_artifact_id']}/download", {}),
    ):
        assert api.get(path, headers=other_headers, params=params).status_code == 404
    with Session(migrated_engine) as session:
        exposures = session.scalars(
            select(DataUsageExposure).where(
                DataUsageExposure.purpose == "result_preview"
            )
        ).all()
        assert {str(ref.evaluation_id) for ref in pair.refs} <= {
            row.provenance.get("evaluation_id") for row in exposures
        }
    if fault == "selected_source":
        wrong = copy.deepcopy(pair.body)
        wrong[side]["source_id"] = str(uuid4())
        response = api.post(
            "/api/v1/comparisons",
            headers=owner_headers | {"Idempotency-Key": "wrong-selected-source"},
            json=wrong,
        )
        assert response.status_code in {404, 409}
        return
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, pair.refs[0 if side == "left" else 1].run_id)
        attempt = session.get(ExecutionAttempt, run.active_attempt_id)
        if fault == "pending":
            run.status, run.result = "running", None
        elif fault == "retry":
            run.generation += 1
        elif fault == "unfinished_attempt":
            attempt.finished_at = None
        else:
            run.result = {"evaluation_id": str(uuid4())}
        session.commit()
    for path, params in (
        (base + "/result", {}),
        (base + "/sections/events", {"manifest_sha256": status["manifest_sha256"]}),
        (f"/api/v1/artifacts/{status['manifest_artifact_id']}/download", {}),
    ):
        response = api.get(path, headers=owner_headers, params=params)
        assert response.status_code == 409, (side, fault, path, response.text)
    for key in ("portable-final-comparison", "portable-final-cache-alias"):
        response = api.post(
            "/api/v1/comparisons",
            headers=owner_headers | {"Idempotency-Key": key},
            json=pair.body,
        )
        assert response.status_code == 409, response.text
